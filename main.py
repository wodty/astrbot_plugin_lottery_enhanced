import asyncio
import re
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.star_tools import StarTools

from .core.lottery import (
    NONE_LEVEL,
    ROLE_NAMES,
    LotteryManager,
    LotteryPersistence,
)
from .utils import get_member_info, get_nickname


class LotteryPlugin(Star):
    """抽奖插件 v2.2 — 支持动态奖品类目 / 等级限制 / 满人自动开奖 / 定时自动开奖 / 批量抽奖"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.lottery_data_file = (
            StarTools.get_data_dir("astrbot_plugin_lottery") / "lottery_data.json"
        )
        self.persistence = LotteryPersistence(str(self.lottery_data_file))
        self.manager = LotteryManager(self.persistence, config)

        # 存储每个群的 unified_msg_origin（用于定时开奖主动发消息）
        self._umos: dict[str, str] = {}
        # 定时开奖后台任务
        self._auto_draw_tasks: dict[str, asyncio.Task] = {}
        # 记录已确认强制开奖的群（人数未满时需二次确认）
        self._force_draw_pending: set[str] = set()

        # 恢复定时开奖任务
        self._restore_auto_draw_tasks()

    # ==================== 内部方法 ====================

    @staticmethod
    def _get_args(event: AstrMessageEvent, command: str) -> str:
        """从消息中提取参数，自动去除命令名前缀。

        AstrBot 的 @filter.command 不会从 event.message_str 中剥离命令名，
        所以这里手动处理：去掉开头的命令名（如果有）。
        """
        text = event.message_str.strip()
        # 去掉可能的 / 前缀
        if text.startswith("/"):
            text = text[1:].strip()
        # 去掉命令名前缀
        if text.startswith(command):
            text = text[len(command):].strip()
        return text

    def _restore_auto_draw_tasks(self):
        """从持久化数据恢复定时开奖任务"""
        for group_id, activity in self.manager.activities.items():
            if activity.is_active and not activity.is_drawn and activity.end_time:
                try:
                    loop = asyncio.get_event_loop()
                    task = loop.create_task(self._schedule_auto_draw(group_id))
                    self._auto_draw_tasks[group_id] = task
                    logger.debug(f"[Lottery] 恢复群 {group_id} 的定时开奖任务")
                except RuntimeError:
                    pass

    async def _schedule_auto_draw(self, group_id: str):
        """定时开奖后台任务"""
        try:
            activity = self.manager.activities.get(group_id)
            if not activity or not activity.end_time:
                return

            end_dt = datetime.fromisoformat(activity.end_time)
            now = datetime.now()
            delay = (end_dt - now).total_seconds()

            if delay > 0:
                await asyncio.sleep(delay)

            activity = self.manager.activities.get(group_id)
            if not activity or not activity.is_active or activity.is_drawn:
                return

            # 没有奖品时不自动开奖，通知群内
            if not activity.has_prizes():
                await self._send_group_message(
                    group_id,
                    "⏰ 抽奖时间已到，但没有配置任何奖品，无法开奖。\n"
                    "管理员请使用「设置奖品」配置后再手动开奖。"
                )
                return

            results = self.manager.batch_draw(group_id)
            if not results:
                return

            msg = self.manager.format_draw_results(group_id, results)
            await self._send_group_message(group_id, f"⏰ 抽奖时间已到，自动开奖！\n\n{msg}")

        except asyncio.CancelledError:
            logger.debug(f"[Lottery] 群 {group_id} 定时开奖任务已取消")
        except Exception as e:
            logger.error(f"[Lottery] 定时开奖任务异常: {e}")

    async def _send_group_message(self, group_id: str, message: str):
        """主动发送群消息（多级降级）"""
        message_chain = MessageChain().message(message)

        umo = self._umos.get(group_id)
        if umo:
            try:
                await self.context.send_message(umo, message_chain)
                return
            except Exception as e:
                logger.warning(f"[Lottery] send_message(umo) 失败: {e}")

        try:
            await StarTools.send_message_by_id(
                "GroupMessage", group_id, message_chain
            )
            return
        except Exception as e:
            logger.warning(f"[Lottery] send_message_by_id 失败: {e}")

        logger.warning(
            f"[Lottery] 无法主动发送群消息到 {group_id}，"
            "开奖结果已存储，用户查询时可见"
        )

    def _cancel_auto_draw_task(self, group_id: str):
        task = self._auto_draw_tasks.pop(group_id, None)
        if task and not task.done():
            task.cancel()

    def _check_and_auto_draw(self, group_id: str) -> Optional[dict]:
        """检查并执行到期自动开奖（懒检查）"""
        activity = self.manager.activities.get(group_id)
        if not activity or not activity.is_active or activity.is_drawn:
            return None
        if not activity.should_auto_draw():
            return None
        self._cancel_auto_draw_task(group_id)
        return self.manager.batch_draw(group_id)

    # ==================== 管理员命令 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启抽奖")
    async def start_lottery(self, event: AstrMessageEvent):
        """开启抽奖活动

        用法：
          开启抽奖                              — 使用配置默认值
          开启抽奖 人数50                       — 满 50 人自动开奖
          开启抽奖 时间2024-01-01 18:00         — 到时间自动开奖
          开启抽奖 人数50 时间2024-01-01 18:00  — 两个条件，先到先开
        """
        group_id = event.get_group_id()
        message_str = self._get_args(event, "开启抽奖")

        max_participants: Optional[int] = None
        end_time: Optional[str] = None

        m_count = re.search(r"人数\s*(\d+)", message_str)
        if m_count:
            max_participants = int(m_count.group(1))

        m_time = re.search(
            r"时间\s*(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", message_str
        )
        if m_time:
            try:
                dt = datetime.strptime(m_time.group(1).strip(), "%Y-%m-%d %H:%M")
                end_time = dt.isoformat()
            except ValueError:
                yield event.plain_result(
                    "时间格式错误，正确示例：时间2024-01-01 18:00"
                )
                return

        ok, msg = self.manager.start_activity(
            group_id, max_participants, end_time
        )
        if not ok:
            yield event.plain_result(msg)
            return

        try:
            self._umos[group_id] = event.unified_msg_origin
        except Exception:
            pass

        if end_time:
            self._cancel_auto_draw_task(group_id)
            try:
                task = asyncio.create_task(self._schedule_auto_draw(group_id))
                self._auto_draw_tasks[group_id] = task
            except RuntimeError:
                pass

        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加奖品")
    async def add_prize(self, event: AstrMessageEvent):
        """添加新的奖品类目

        用法：添加奖品 <奖项名称> <奖品名称> <数量>
        示例：
          添加奖品 幸运奖 公仔 10
          添加奖品 特别奖 限定手办 3
        """
        message_str = self._get_args(event, "添加奖品")

        # 匹配：奖项名称（无空格）+ 奖品名称（可含空格）+ 数量
        m = re.match(r"(\S+)\s+(.+?)\s+(\d+)\s*$", message_str)
        if not m:
            yield event.plain_result(
                "格式错误\n"
                "正确示例：\n"
                "  添加奖品 幸运奖 公仔 10\n"
                "  添加奖品 特别奖 限定手办 3"
            )
            return

        level_name = m.group(1)
        prize_name = m.group(2)
        count = int(m.group(3))

        ok, msg = self.manager.add_prize(event.get_group_id(), level_name, prize_name, count)
        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除奖品")
    async def remove_prize(self, event: AstrMessageEvent, level_name: str = ""):
        """删除奖品类目

        用法：删除奖品 <奖项名称>
        示例：删除奖品 参与奖
        """
        if not level_name:
            level_name = self._get_args(event, "删除奖品")
        if not level_name:
            yield event.plain_result("请指定要删除的奖项名称，示例：删除奖品 参与奖")
            return

        ok, msg = self.manager.remove_prize(event.get_group_id(), level_name)
        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置奖品")
    async def set_prize(self, event: AstrMessageEvent):
        """修改已有奖品类目的奖品名称和数量

        用法：设置奖品 <奖项名称> <奖品名称> <数量>
        示例：
          设置奖品 特等奖 iPhone15 2
          设置奖品 一等奖 蓝牙耳机 5
          设置奖品 幸运奖 公仔 20
        """
        message_str = self._get_args(event, "设置奖品")

        # 匹配：奖项名称（无空格）+ 奖品名称（可含空格）+ 数量
        m = re.match(r"(\S+)\s+(.+?)\s+(\d+)\s*$", message_str)
        if not m:
            yield event.plain_result(
                "格式错误\n"
                "正确示例：\n"
                "  设置奖品 特等奖 iPhone15 2\n"
                "  设置奖品 幸运奖 公仔 20\n"
                "  设置奖品 一等奖 我的奖品 5"
            )
            return

        level_name = m.group(1)
        prize_name = m.group(2)
        count = int(m.group(3))

        ok, msg = self.manager.set_prize(event.get_group_id(), level_name, prize_name, count)
        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置等级")
    async def set_level(self, event: AstrMessageEvent, arg: str = ""):
        """设置参与抽奖的等级要求

        用法：
          设置等级 5         — 最低群等级 Lv.5
          设置等级 管理员     — 最低身份管理员
          设置等级 群主       — 最低身份群主
          设置等级 取消       — 取消所有限制
        """
        group_id = event.get_group_id()
        # AstrBot 原生参数解析 + _get_args 双保险
        if not arg:
            arg = self._get_args(event, "设置等级")

        min_level = 0
        min_role = "member"

        if arg in ("取消", "0"):
            min_level, min_role = 0, "member"
        elif arg == "管理员":
            min_role = "admin"
        elif arg == "群主":
            min_role = "owner"
        elif arg.isdigit():
            min_level = int(arg)
        else:
            yield event.plain_result(
                "格式错误\n"
                "正确示例：\n"
                "  设置等级 5（最低群等级）\n"
                "  设置等级 管理员（最低身份）\n"
                "  设置等级 取消（取消限制）"
            )
            return

        ok, msg = self.manager.set_level_requirement(group_id, min_level, min_role)
        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开奖")
    async def manual_draw(self, event: AstrMessageEvent):
        """手动开奖（管理员）"""
        group_id = event.get_group_id()
        activity = self.manager.activities.get(group_id)
        if not activity:
            yield event.plain_result("当前群没有抽奖活动")
            return
        if activity.is_drawn:
            yield event.plain_result("抽奖已经开过了，请发送「中奖名单」查看结果")
            return
        if not activity.participants:
            yield event.plain_result("暂无参与者，无法开奖")
            return

        # 检查是否有可用奖品
        if not activity.has_prizes():
            yield event.plain_result(
                "⚠️ 当前没有任何奖品配置（所有奖品数量为 0）\n"
                "请先使用「设置奖品」或「添加奖品」配置奖品后再开奖"
            )
            return

        # 如果设了满人条件但人数未满，需要二次确认
        if (
            activity.max_participants > 0
            and len(activity.participants) < activity.max_participants
            and group_id not in self._force_draw_pending
        ):
            self._force_draw_pending.add(group_id)
            yield event.plain_result(
                f"⚠️ 当前 {len(activity.participants)} 人参与，"
                f"目标 {activity.max_participants} 人尚未满\n"
                f"再次发送「开奖」可强制开奖"
            )
            return

        # 清除强制开奖标记
        self._force_draw_pending.discard(group_id)

        self._cancel_auto_draw_task(group_id)
        results = self.manager.batch_draw(group_id)
        if results:
            msg = self.manager.format_draw_results(group_id, results)
            yield event.plain_result(msg)
        else:
            yield event.plain_result("开奖失败")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭抽奖")
    async def stop_lottery(self, event: AstrMessageEvent):
        """关闭抽奖活动"""
        group_id = event.get_group_id()
        self._cancel_auto_draw_task(group_id)
        _, msg = self.manager.stop_activity(group_id)
        yield event.plain_result(msg)

    @filter.command("重置抽奖")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_lottery(self, event: AstrMessageEvent):
        """重置本群抽奖活动"""
        group_id = event.get_group_id()
        self._cancel_auto_draw_task(group_id)
        ok = self.manager.delete_activity(group_id)
        yield event.plain_result("本群抽奖已清空，可重新开启" if ok else "当前无抽奖可重置")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("抽奖配置")
    async def lottery_config(self, event: AstrMessageEvent):
        """查看所有抽奖插件配置信息"""
        info = self.manager.get_config_info()

        lines = ["⚙️ 抽奖插件配置", ""]

        # 默认奖品配置
        lines.append("🎁 默认奖品配置：")
        for p in info["prizes"]:
            lines.append(f"  {p['emoji']} {p['level']}：{p['name']} x{p['count']}")
        lines.append("")

        # 默认抽奖设置
        lines.append("📊 默认抽奖设置：")
        if info["default_max_participants"] > 0:
            lines.append(f"  默认满人开奖：{info['default_max_participants']} 人")
        else:
            lines.append("  默认开奖方式：手动开奖")
        if info["default_min_level"] > 0:
            lines.append(f"  默认最低等级：Lv.{info['default_min_level']}")
        else:
            lines.append("  默认最低等级：不限")
        lines.append(f"  默认最低身份：{ROLE_NAMES.get(info['default_min_role'], info['default_min_role'])}")
        lines.append("")

        # 开启提示配置
        lines.append("📢 开启提示配置：")
        lines.append(f"  自定义提示：{'已启用' if info['notice_enabled'] else '已关闭'}")
        if info["notice_template"]:
            lines.append("  消息模板：")
            lines.append(f"    {info['notice_template']}")
        else:
            lines.append("  消息模板：（使用默认模板）")

        yield event.plain_result("\n".join(lines))

    # ==================== 用户命令 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("参加抽奖")
    async def participate_lottery(self, event: AstrMessageEvent):
        """参加抽奖"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        nickname = await get_nickname(event, user_id)
        level, role = await get_member_info(event, user_id)

        msg, success, should_draw = self.manager.participate(
            group_id, user_id, nickname, level, role
        )

        if should_draw:
            results = self.manager.batch_draw(group_id)
            if results:
                draw_msg = self.manager.format_draw_results(group_id, results)
                yield event.plain_result(f"{msg}\n\n{draw_msg}")
            else:
                yield event.plain_result(
                    f"{msg}\n\n开奖失败，请联系管理员手动开奖"
                )
            return

        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽奖")
    async def draw_lottery(self, event: AstrMessageEvent):
        """参与抽奖（兼容旧命令，等同「参加抽奖」）"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        nickname = await get_nickname(event, user_id)
        level, role = await get_member_info(event, user_id)

        msg, success, should_draw = self.manager.participate(
            group_id, user_id, nickname, level, role
        )

        if should_draw:
            results = self.manager.batch_draw(group_id)
            if results:
                draw_msg = self.manager.format_draw_results(group_id, results)
                yield event.plain_result(f"{msg}\n\n{draw_msg}")
            else:
                yield event.plain_result(
                    f"{msg}\n\n开奖失败，请联系管理员手动开奖"
                )
            return

        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽奖状态")
    async def lottery_status(self, event: AstrMessageEvent):
        """查看抽奖状态"""
        group_id = event.get_group_id()

        auto_results = self._check_and_auto_draw(group_id)
        if auto_results:
            msg = self.manager.format_draw_results(group_id, auto_results)
            yield event.plain_result(f"⏰ 抽奖时间已到，自动开奖！\n\n{msg}")
            return

        data = self.manager.get_status_and_winners(group_id)
        if not data:
            yield event.plain_result("当前群聊没有抽奖活动")
            return

        ov = data["overview"]
        if ov["active"]:
            status_text = "🟢 进行中"
        elif ov["drawn"]:
            status_text = "🔴 已开奖"
        else:
            status_text = "⚪ 已结束"

        lines = [
            f"📊 本群抽奖活动【{status_text}】",
            f"参与 {ov['participants']} 人　中奖 {ov['winners']} 人",
            "🎁 奖品剩余：",
        ]
        lines += [
            f"  {p['name']}：{p['remaining']}/{p['total']}" for p in data["prize_left"]
        ]
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽奖详情")
    async def lottery_detail(self, event: AstrMessageEvent):
        """查看抽奖详细信息（奖品、时间、人数、参加名单）"""
        group_id = event.get_group_id()

        auto_results = self._check_and_auto_draw(group_id)
        if auto_results:
            msg = self.manager.format_draw_results(group_id, auto_results)
            yield event.plain_result(f"⏰ 抽奖时间已到，自动开奖！\n\n{msg}")
            return

        detail = self.manager.get_activity_detail(group_id)
        if not detail:
            yield event.plain_result("当前群聊没有抽奖活动")
            return

        lines = ["📋 抽奖详情", ""]

        if detail["active"]:
            status = "🟢 进行中"
        elif detail["drawn"]:
            status = "🔴 已开奖"
        else:
            status = "⚪ 已结束"
        lines.append(f"📌 状态：{status}")

        lines.append(f"🕐 创建时间：{detail['created_at']}")
        if detail["end_time"]:
            lines.append(f"⏰ 开奖时间：{detail['end_time']}（到期自动开奖）")

        participant_info = f"👤 参与人数：{detail['participant_count']}"
        if detail["max_participants"] > 0:
            participant_info += f" / {detail['max_participants']}（满人自动开奖）"
        lines.append(participant_info)

        req_parts = []
        if detail["min_level"] > 0:
            req_parts.append(f"Lv.{detail['min_level']}")
        if detail["min_role"] != "member":
            req_parts.append(ROLE_NAMES.get(detail["min_role"], detail["min_role"]))
        if req_parts:
            lines.append(f"🏅 参与要求：{' / '.join(req_parts)}")
        else:
            lines.append("🏅 参与要求：无限制")

        lines.append("")
        lines.append("🎁 奖品设置：")
        for p in detail["prize_config"]:
            lines.append(
                f"  {p['emoji']} {p['level']}（{p['name']}）：{p['remaining']}/{p['total']}"
            )

        lines.append("")
        if detail["participants"]:
            lines.append(f"👥 参与名单（{detail['participant_count']} 人）：")
            participants = detail["participants"]
            for i in range(0, len(participants), 5):
                lines.append("  " + "、".join(participants[i : i + 5]))
        else:
            lines.append("👥 暂无参与者")

        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("中奖名单")
    async def winner_list(self, event: AstrMessageEvent):
        """查看中奖名单"""
        group_id = event.get_group_id()
        activity = self.manager.activities.get(group_id)
        if not activity:
            yield event.plain_result("当前群聊没有抽奖活动")
            return

        if not activity.is_drawn:
            yield event.plain_result("抽奖尚未开奖，请等待自动开奖或管理员手动开奖")
            return

        data = self.manager.get_status_and_winners(group_id)
        if not data or not data["winners_by_lvl"]:
            yield event.plain_result("暂无中奖者")
            return

        lines = ["🏆 中奖名单："]
        # 按活动奖品顺序显示
        for level_name in activity.prize_order:
            if level_name in data["winners_by_lvl"]:
                uids = data["winners_by_lvl"][level_name]
                user_names = [activity.participants.get(uid, uid) for uid in uids]
                emoji = activity.prize_config[level_name]["emoji"]
                prize_name = activity.prize_config[level_name]["name"]
                lines.append(f"{emoji} {level_name}（{prize_name}）：{'、'.join(user_names)}")
        yield event.plain_result("\n".join(lines))

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件终止时，取消所有定时任务"""
        for group_id in list(self._auto_draw_tasks.keys()):
            self._cancel_auto_draw_task(group_id)
        logger.info("抽奖插件已终止")
