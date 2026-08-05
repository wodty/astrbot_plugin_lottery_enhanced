import random
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from .data import LotteryPersistence

# ---------- 常量 ----------

ROLE_ORDER = {"member": 0, "admin": 1, "owner": 2}
ROLE_NAMES = {"member": "群成员", "admin": "管理员", "owner": "群主"}

# 内置奖项等级的 emoji 映射
PRIZE_EMOJIS = {
    "特等奖": "🎊",
    "一等奖": "🥇",
    "二等奖": "🥈",
    "三等奖": "🥉",
    "参与奖": "🎁",
    "未中奖": "😢",
}

DEFAULT_PRIZE_EMOJI = "🏆"
NONE_LEVEL = "未中奖"

# 旧版枚举名 → 中文名（用于数据迁移）
_ENUM_NAME_TO_LEVEL = {
    "SPECIAL": "特等奖",
    "FIRST": "一等奖",
    "SECOND": "二等奖",
    "THIRD": "三等奖",
    "PARTICIPATE": "参与奖",
    "NONE": "未中奖",
}

# 配置 key → 奖项中文名
_CONFIG_KEY_TO_LEVEL = {
    "special": "特等奖",
    "first": "一等奖",
    "second": "二等奖",
    "third": "三等奖",
    "participate": "参与奖",
}


def get_prize_emoji(level_name: str) -> str:
    """获取奖项等级的 emoji"""
    return PRIZE_EMOJIS.get(level_name, DEFAULT_PRIZE_EMOJI)


class LotteryActivity:
    """抽奖活动类（v2.2 — 支持动态奖品类目 / 等级限制 / 满人自动开奖 / 定时自动开奖 / 批量抽奖）"""

    def __init__(
        self,
        group_id: str,
        template: dict[str, dict],
        max_participants: int = 0,
        end_time: Optional[str] = None,
        min_level: int = 0,
        min_role: str = "member",
        skip_template: bool = False,
    ):
        self.group_id = group_id
        self.is_active = False
        self.is_drawn = False
        self.created_at = datetime.now().isoformat()
        self.end_time = end_time
        self.max_participants = max_participants
        self.min_level = min_level
        self.min_role = min_role
        self.participants: dict[str, str] = {}
        self.winners: dict[str, str] = {}  # user_id -> level_name

        # 奖品类目：有序列表 + 配置字典
        self.prize_order: list[str] = []  # 奖项等级名称列表（开奖顺序，从高到低）
        self.prize_config: dict[str, dict] = {}  # level_name -> {name, count, remaining, emoji}

        # 从模板初始化默认奖品类目（skip_template=True 时跳过，开启时为空白）
        if not skip_template:
            for level_name, cfg in template.items():
                self._init_prize_level(level_name, cfg["name"], cfg["count"])

    def _init_prize_level(self, level_name: str, prize_name: str, count: int):
        """初始化一个奖品类目（内部方法，不检查重复）"""
        self.prize_order.append(level_name)
        self.prize_config[level_name] = {
            "name": prize_name,
            "count": count,
            "remaining": count,
            "emoji": get_prize_emoji(level_name),
        }

    # ---------- 奖品类目管理 ----------

    def add_prize_level(
        self, level_name: str, prize_name: str, count: int
    ) -> tuple[bool, str]:
        """添加奖品类目"""
        if level_name == NONE_LEVEL:
            return False, f"不能使用「{NONE_LEVEL}」作为奖项名称"
        if level_name in self.prize_config:
            return False, f"奖项「{level_name}」已存在，请使用「设置奖品」修改"
        if count < 0:
            return False, "奖品数量不能为负数"
        if not prize_name.strip():
            return False, "奖品名称不能为空"

        self._init_prize_level(level_name, prize_name.strip(), count)
        return True, (
            f"{get_prize_emoji(level_name)} 已添加奖项「{level_name}」：\n"
            f"  奖品名称：{prize_name.strip()}\n"
            f"  奖品数量：{count}"
        )

    def remove_prize_level(self, level_name: str) -> tuple[bool, str]:
        """删除奖品类目"""
        if level_name not in self.prize_config:
            return False, f"奖项「{level_name}」不存在"
        if len(self.prize_order) <= 1:
            return False, "至少需要保留一个奖项，无法删除"

        cfg = self.prize_config[level_name]
        del self.prize_config[level_name]
        self.prize_order.remove(level_name)
        return True, (
            f"{cfg['emoji']} 已删除奖项「{level_name}」\n"
            f"  （原奖品：{cfg['name']} x{cfg['count']}）"
        )

    def set_prize_level(
        self, level_name: str, prize_name: str, count: int
    ) -> tuple[bool, str]:
        """修改已有奖品类目的奖品名称和数量"""
        if level_name not in self.prize_config:
            return False, f"奖项「{level_name}」不存在，请使用「添加奖品 {level_name} 奖品名称 数量」添加"
        if count < 0:
            return False, "奖品数量不能为负数"
        if not prize_name.strip():
            return False, "奖品名称不能为空"

        old_name = self.prize_config[level_name]["name"]
        old_count = self.prize_config[level_name]["count"]
        self.prize_config[level_name] = {
            "count": count,
            "remaining": count,
            "name": prize_name.strip(),
            "emoji": self.prize_config[level_name]["emoji"],
        }
        return True, (
            f"{self.prize_config[level_name]['emoji']} {level_name} 奖品已更新：\n"
            f"  名称：{old_name} → {prize_name.strip()}\n"
            f"  数量：{old_count} → {count}"
        )

    # ---------- 参与者管理 ----------

    def add_participant(self, user_id: str, nickname: str) -> bool:
        """添加参与者"""
        if user_id not in self.participants:
            self.participants[user_id] = nickname
            return True
        return False

    def has_participated(self, user_id: str) -> bool:
        return user_id in self.participants

    # ---------- 等级检查 ----------

    def check_level(self, level: int, role: str) -> tuple[bool, str]:
        """检查成员是否满足等级 / 身份要求"""
        if self.min_role != "member":
            if ROLE_ORDER.get(role, 0) < ROLE_ORDER.get(self.min_role, 0):
                return False, (
                    f"⚠️ 您的身份不满足要求\n"
                    f"需要：{ROLE_NAMES.get(self.min_role, self.min_role)} 及以上\n"
                    f"当前：{ROLE_NAMES.get(role, role)}"
                )
        if self.min_level > 0 and level < self.min_level:
            return False, (
                f"⚠️ 您的群等级不满足要求\n"
                f"需要：Lv.{self.min_level}\n"
                f"当前：Lv.{level}"
            )
        return True, ""

    # ---------- 自动开奖判断 ----------

    def should_auto_draw(self) -> bool:
        """检查是否应该触发自动开奖"""
        if not self.is_active or self.is_drawn:
            return False
        # 没有奖品时不自动开奖
        if not self.has_prizes():
            return False
        if self.max_participants > 0 and len(self.participants) >= self.max_participants:
            return True
        if self.end_time:
            try:
                end_dt = datetime.fromisoformat(self.end_time)
                if datetime.now() >= end_dt:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    # ---------- 批量开奖 ----------

    def has_prizes(self) -> bool:
        """检查是否有可用奖品（至少一个类目数量 > 0）"""
        return any(cfg["count"] > 0 for cfg in self.prize_config.values())

    def batch_draw(self) -> dict[str, str]:
        """批量开奖：从所有参与者中按 prize_order 顺序随机抽取

        - 按 prize_order 从高到低依次抽取
        - 每个等级抽取 min(count, 剩余人数) 个中奖者
        - 中奖者从候选池中移除，不重复中奖
        - 剩余参与者标记为 NONE_LEVEL
        """
        all_participants = list(self.participants.keys())
        random.shuffle(all_participants)

        results: dict[str, str] = {}
        remaining = set(all_participants)

        for level_name in self.prize_order:
            cfg = self.prize_config[level_name]
            count = min(cfg["count"], len(remaining))
            if count <= 0:
                continue
            winners = random.sample(list(remaining), count)
            for w in winners:
                results[w] = level_name
                self.winners[w] = level_name
                cfg["remaining"] -= 1
            remaining -= set(winners)

        # 剩余的人未中奖
        for uid in remaining:
            results[uid] = NONE_LEVEL

        self.is_drawn = True
        self.is_active = False
        return results

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "is_active": self.is_active,
            "is_drawn": self.is_drawn,
            "created_at": self.created_at,
            "end_time": self.end_time,
            "max_participants": self.max_participants,
            "min_level": self.min_level,
            "min_role": self.min_role,
            "participants": self.participants,
            "winners": self.winners,
            "prize_order": self.prize_order,
            "prize_config": dict(self.prize_config),
        }

    @classmethod
    def from_dict(cls, data: dict, template: dict[str, dict]) -> "LotteryActivity":
        activity = cls(
            data["group_id"],
            template,
            max_participants=data.get("max_participants", 0),
            end_time=data.get("end_time"),
            min_level=data.get("min_level", 0),
            min_role=data.get("min_role", "member"),
        )
        activity.is_active = data["is_active"]
        activity.is_drawn = data.get("is_drawn", False)
        activity.created_at = data["created_at"]
        activity.participants = data["participants"]
        activity.winners = data["winners"]

        # 恢复奖品类目（兼容旧版枚举 key 和新版中文 key）
        saved_config: dict[str, dict] = data.get("prize_config", {})
        saved_order: list[str] = data.get("prize_order", [])

        if saved_config:
            # 清空模板初始化的类目，用保存的数据替换
            activity.prize_order = []
            activity.prize_config = {}

            # 确定顺序：优先用保存的 prize_order，否则从 config keys 推断
            if saved_order:
                ordered_keys = saved_order
            else:
                # 旧版数据没有 prize_order，按枚举顺序推断
                ordered_keys = list(saved_config.keys())

            for key in ordered_keys:
                cfg = saved_config.get(key)
                if cfg is None:
                    continue

                # 旧版枚举名 → 中文名
                level_name = _ENUM_NAME_TO_LEVEL.get(key, key)

                activity.prize_order.append(level_name)
                activity.prize_config[level_name] = {
                    "name": cfg.get("name", ""),
                    "count": cfg.get("count", 0),
                    "remaining": cfg.get("remaining", cfg.get("count", 0)),
                    "emoji": cfg.get("emoji", get_prize_emoji(level_name)),
                }

            # 补充 saved_config 中存在但 order 中不存在的项
            for key, cfg in saved_config.items():
                level_name = _ENUM_NAME_TO_LEVEL.get(key, key)
                if level_name not in activity.prize_config:
                    activity.prize_order.append(level_name)
                    activity.prize_config[level_name] = {
                        "name": cfg.get("name", ""),
                        "count": cfg.get("count", 0),
                        "remaining": cfg.get("remaining", cfg.get("count", 0)),
                        "emoji": cfg.get("emoji", get_prize_emoji(level_name)),
                    }

        return activity


class LotteryManager:
    """抽奖管理类"""

    def __init__(self, persistence: LotteryPersistence, config: AstrBotConfig):
        self.activities: dict[str, LotteryActivity] = {}

        # 解析奖品模板（v2.2: 从 prize_config 读取，兼容旧 default_prize_config）
        prize_raw = config.get("prize_config") or config.get("default_prize_config") or {}
        self.template: dict[str, dict] = {}
        for k, v in prize_raw.items():
            level_name = _CONFIG_KEY_TO_LEVEL.get(k, k)
            self.template[level_name] = {
                "name": v.get("name", ""),
                "count": v.get("count", 0),
            }

        # 如果配置未加载（AstrBot 未自动填充默认值），使用硬编码默认值
        if not self.template:
            self.template = {
                "一等奖": {"name": "U盘", "count": 3, "color": "#D91E1C"},
                "二等奖": {"name": "小风扇", "count": 5, "color": "#007CC2"},
                "三等奖": {"name": "钥匙扣", "count": 10, "color": "#DD167B"},
            }
            logger.info("[Lottery] 配置未加载，使用硬编码默认奖品模板")
        else:
            # 检查是否有任何奖品数量 > 0，如果全是 0 也用默认值
            has_prizes = any(cfg["count"] > 0 for cfg in self.template.values())
            if not has_prizes:
                self.template = {
                    "一等奖": {"name": "U盘", "count": 3, "color": "#D91E1C"},
                    "二等奖": {"name": "小风扇", "count": 5, "color": "#007CC2"},
                    "三等奖": {"name": "钥匙扣", "count": 10, "color": "#DD167B"},
                }
                logger.info("[Lottery] 所有奖品数量为 0，使用硬编码默认奖品模板")

        # 读取默认抽奖设置
        settings = config.get("lottery_settings") or {}
        self.default_max_participants = settings.get("default_max_participants", 0)
        self.default_min_level = settings.get("default_min_level", 0)
        self.default_min_role = settings.get("default_min_role", "member")

        self.persistence = persistence
        self.persistence.load(self)

    # ---------- 活动生命周期 ----------

    def start_activity(
        self,
        group_id: str,
        max_participants: Optional[int] = None,
        end_time: Optional[str] = None,
        min_level: Optional[int] = None,
        min_role: Optional[str] = None,
    ) -> tuple[bool, str]:
        """开启抽奖活动（参数为 None 时使用配置默认值）"""
        if self.activities.get(group_id) and self.activities[group_id].is_active:
            return False, "该群已有进行中的抽奖活动"

        if max_participants is None:
            max_participants = self.default_max_participants
        if min_level is None:
            min_level = self.default_min_level
        if min_role is None:
            min_role = self.default_min_role

        # 创建全新活动（skip_template=True：不加载默认奖品，管理员手动添加）
        self.activities[group_id] = LotteryActivity(
            group_id, self.template, max_participants, end_time, min_level, min_role,
            skip_template=True,
        )
        self.activities[group_id].is_active = True
        self.persistence.save(self)

        # ---- 构建提示消息 ----

        # 开奖条件
        conditions_parts = []
        if max_participants > 0:
            conditions_parts.append(f"满 {max_participants} 人自动开奖")
        if end_time:
            conditions_parts.append(f"{end_time} 自动开奖")
        if conditions_parts:
            conditions_str = "、".join(conditions_parts)
        else:
            conditions_str = "手动开奖（发送「开奖」开奖）"

        msg = f"✅ 抽奖活动已开启（{conditions_str}）"
        if min_level > 0 or min_role != "member":
            req_parts = []
            if min_level > 0:
                req_parts.append(f"Lv.{min_level}")
            if min_role != "member":
                req_parts.append(ROLE_NAMES.get(min_role, min_role))
            msg += f"\n🏅 参与要求：{' / '.join(req_parts)}"
        msg += (
            "\n\n⚠️ 当前还没有配置任何奖品！\n"
            "请先添加奖项后再让群友参与：\n"
            "  添加奖品 一等奖 iPhone15 2\n"
            "  添加奖品 二等奖 蓝牙耳机 5\n"
            "  添加奖品 三等奖 钥匙扣 10\n"
            "\n💡 添加完成后，发送「参加抽奖」即可参与"
        )
        return True, msg

    def stop_activity(self, group_id: str) -> tuple[bool, str]:
        if group_id not in self.activities:
            return False, "该群没有抽奖活动"
        activity = self.activities[group_id]
        if not activity.is_active:
            return False, "抽奖活动已经停止"
        activity.is_active = False
        self.persistence.save(self)
        return True, "抽奖活动已停止"

    def delete_activity(self, group_id: str) -> bool:
        if group_id not in self.activities:
            return False
        del self.activities[group_id]
        self.persistence.save(self)
        return True

    # ---------- 参与抽奖 ----------

    def participate(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        level: int = 0,
        role: str = "member",
    ) -> tuple[str, bool, bool]:
        if group_id not in self.activities:
            return "该群没有抽奖活动", False, False

        activity = self.activities[group_id]
        if not activity.is_active:
            return "抽奖活动未开启", False, False
        if activity.is_drawn:
            return "抽奖已开奖，请发送「中奖名单」查看结果", False, False
        if not activity.has_prizes():
            return "⚠️ 管理员还未配置奖品，暂时无法参与抽奖\n请稍后再试", False, False
        if activity.has_participated(user_id):
            return "您已经参加过了", False, False

        ok, msg = activity.check_level(level, role)
        if not ok:
            return msg, False, False

        activity.add_participant(user_id, nickname)
        self.persistence.save(self)

        should_draw = activity.should_auto_draw()

        feedback = f"✅ 参加成功！您是第 {len(activity.participants)} 位参与者"
        if activity.max_participants > 0:
            feedback += f"（目标 {activity.max_participants} 人）"
        if activity.end_time:
            feedback += f"\n⏰ 开奖时间：{activity.end_time}"
        if should_draw:
            feedback += "\n🎉 人数已满，正在自动开奖..."

        return feedback, True, should_draw

    # ---------- 开奖 ----------

    def has_prizes(self, group_id: str) -> bool:
        """检查活动是否有可用奖品"""
        activity = self.activities.get(group_id)
        if not activity:
            return False
        return activity.has_prizes()

    def batch_draw(self, group_id: str) -> Optional[dict[str, str]]:
        activity = self.activities.get(group_id)
        if not activity or activity.is_drawn:
            return None
        results = activity.batch_draw()
        self.persistence.save(self)
        return results

    def format_draw_results(
        self, group_id: str, results: dict[str, str]
    ) -> str:
        activity = self.activities.get(group_id)
        if not activity:
            return "抽奖活动不存在"

        lines = ["🎉🎉🎉 抽奖结果出炉！🎉🎉🎉", ""]
        total = len(results)
        lines.append(f"📊 共 {total} 人参与抽奖")
        lines.append("")

        for level_name in activity.prize_order:
            cfg = activity.prize_config[level_name]
            if cfg["count"] <= 0:
                continue
            winners = [
                activity.participants[uid]
                for uid, lvl in results.items()
                if lvl == level_name
            ]
            if winners:
                lines.append(
                    f"{cfg['emoji']} {level_name}（{cfg['name']}）：{'、'.join(winners)}"
                )
            else:
                lines.append(f"{cfg['emoji']} {level_name}（{cfg['name']}）：无人中奖")

        no_prize = [
            activity.participants[uid]
            for uid, lvl in results.items()
            if lvl == NONE_LEVEL
        ]
        if no_prize:
            lines.append("")
            lines.append(f"😢 未中奖（{len(no_prize)} 人）")

        lines.append("")
        lines.append("恭喜以上中奖者！🎊")
        return "\n".join(lines)

    # ---------- 奖品类目操作 ----------

    def add_prize(
        self, group_id: str, level_name: str, prize_name: str, count: int
    ) -> tuple[bool, str]:
        """添加奖品类目"""
        activity = self.activities.get(group_id)
        if not activity:
            return False, "当前群没有抽奖活动"
        if not activity.is_active:
            return False, "抽奖活动未开启，请先「开启抽奖」"
        if activity.is_drawn:
            return False, "抽奖已开奖，无法修改奖品"

        ok, msg = activity.add_prize_level(level_name, prize_name, count)
        if ok:
            self.persistence.save(self)
        return ok, msg

    def remove_prize(self, group_id: str, level_name: str) -> tuple[bool, str]:
        """删除奖品类目"""
        activity = self.activities.get(group_id)
        if not activity:
            return False, "当前群没有抽奖活动"
        if not activity.is_active:
            return False, "抽奖活动未开启"
        if activity.is_drawn:
            return False, "抽奖已开奖，无法修改奖品"

        ok, msg = activity.remove_prize_level(level_name)
        if ok:
            self.persistence.save(self)
        return ok, msg

    def set_prize(
        self, group_id: str, level_name: str, prize_name: str, count: int
    ) -> tuple[bool, str]:
        """修改奖品类目的奖品名称和数量"""
        activity = self.activities.get(group_id)
        if not activity:
            return False, "当前群没有抽奖活动"
        if not activity.is_active:
            return False, "抽奖活动未开启，请先「开启抽奖」"
        if activity.is_drawn:
            return False, "抽奖已开奖，无法修改奖品"

        ok, msg = activity.set_prize_level(level_name, prize_name, count)
        if ok:
            self.persistence.save(self)
        return ok, msg

    # ---------- 等级设置 ----------

    def set_level_requirement(
        self, group_id: str, min_level: int = 0, min_role: str = "member"
    ) -> tuple[bool, str]:
        activity = self.activities.get(group_id)
        if not activity or not activity.is_active:
            return False, "当前群没有进行中的抽奖活动"
        activity.min_level = min_level
        activity.min_role = min_role
        self.persistence.save(self)
        if min_level == 0 and min_role == "member":
            return True, "✅ 已取消参与等级限制"
        parts = []
        if min_level > 0:
            parts.append(f"最低群等级 Lv.{min_level}")
        if min_role != "member":
            parts.append(f"最低身份 {ROLE_NAMES.get(min_role, min_role)}")
        return True, f"✅ 参与要求已设置：{' / '.join(parts)}"

    # ---------- 查询 ----------

    def get_status_and_winners(self, group_id: str) -> Optional[dict]:
        activity = self.activities.get(group_id)
        if not activity:
            return None

        overview = {
            "active": activity.is_active,
            "drawn": activity.is_drawn,
            "participants": len(activity.participants),
            "winners": len(activity.winners),
        }

        prize_left = []
        for level_name in activity.prize_order:
            cfg = activity.prize_config[level_name]
            if cfg["count"] > 0:
                prize_left.append({
                    "level": level_name,
                    "name": cfg["name"],
                    "remaining": cfg["remaining"],
                    "total": cfg["count"],
                })

        winners_by_lvl: dict[str, list[str]] = {}
        for uid, lvl_name in activity.winners.items():
            winners_by_lvl.setdefault(lvl_name, []).append(uid)

        return {
            "overview": overview,
            "prize_left": prize_left,
            "winners_by_lvl": winners_by_lvl,
        }

    def get_activity_detail(self, group_id: str) -> Optional[dict]:
        """获取活动详细信息"""
        activity = self.activities.get(group_id)
        if not activity:
            return None

        prize_list = []
        for level_name in activity.prize_order:
            cfg = activity.prize_config[level_name]
            prize_list.append({
                "level": level_name,
                "emoji": cfg["emoji"],
                "name": cfg["name"],
                "remaining": cfg["remaining"],
                "total": cfg["count"],
            })

        return {
            "active": activity.is_active,
            "drawn": activity.is_drawn,
            "created_at": activity.created_at,
            "end_time": activity.end_time,
            "max_participants": activity.max_participants,
            "min_level": activity.min_level,
            "min_role": activity.min_role,
            "participant_count": len(activity.participants),
            "participants": list(activity.participants.values()),
            "prize_config": prize_list,
            "winners": activity.winners,
        }

    def get_config_info(self) -> dict:
        """获取所有插件配置信息（供「抽奖配置」命令展示）"""
        # 默认奖品模板
        prizes = []
        for level_name, cfg in self.template.items():
            prizes.append({
                "level": level_name,
                "emoji": get_prize_emoji(level_name),
                "name": cfg["name"],
                "count": cfg["count"],
            })

        return {
            "prizes": prizes,
            "default_max_participants": self.default_max_participants,
            "default_min_level": self.default_min_level,
            "default_min_role": self.default_min_role,
        }
