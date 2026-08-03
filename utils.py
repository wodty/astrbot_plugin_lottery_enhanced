from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


async def get_nickname(event: AstrMessageEvent, user_id: str) -> str:
    """获取指定群友的群昵称或Q名"""
    if event.get_platform_name() == "aiocqhttp" and user_id.isdigit():
        assert isinstance(event, AiocqhttpMessageEvent)
        try:
            all_info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            )
            return all_info.get("card") or all_info.get("nickname") or user_id
        except Exception:
            return user_id
    return user_id


async def get_member_info(event: AstrMessageEvent, user_id: str) -> tuple[int, str]:
    """获取群成员的等级和身份

    Returns:
        (level, role)
        level: 群活跃等级（整数），role: "member" / "admin" / "owner"
        非 aiocqhttp 平台返回 (0, "member")
    """
    if event.get_platform_name() == "aiocqhttp" and user_id.isdigit():
        assert isinstance(event, AiocqhttpMessageEvent)
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            )
            level = int(info.get("level", 0))
            role = info.get("role", "member")
            return level, role
        except Exception:
            return 0, "member"
    return 0, "member"
