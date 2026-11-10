import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Router
from aiogram.types import TelegramObject, Message, CallbackQuery, ChatMemberUpdated, Update

from app.bot.handlers.group_features import track_member_seen
from app.database.db import get_db, ensure_group_initialized

logger = logging.getLogger(__name__)

tracker_router = Router(name="group_tracker_router")


class GroupTrackerMiddleware(BaseMiddleware):
    """
    自动记录与同步 Bot 所在群组信息与激活状态的中间件
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # dp.update.outer_middleware 收到的是 Update 本身, 不是内层事件:
        # 必须先把 Update 拆开再走下面的 isinstance 分支, 否则中间件
        # 永远匹配不上, ensure_group_initialized 一次都不会被调用。
        # 注意: 传给 handler 的仍是原始 event (Update), 这里只用 inner 做分支判断。
        inner: TelegramObject = event
        if isinstance(event, Update):
            for candidate in (
                event.message,
                event.edited_message,
                event.callback_query,
                event.my_chat_member,
                event.chat_member,
            ):
                if candidate is not None:
                    inner = candidate
                    break

        chat = None
        if isinstance(inner, Message):
            chat = inner.chat
        elif isinstance(inner, CallbackQuery) and inner.message:
            chat = inner.message.chat
        elif isinstance(inner, ChatMemberUpdated):
            chat = inner.chat

        if chat and chat.type in ("group", "supergroup"):
            await ensure_group_initialized(chat.id, chat.title or f"Group {chat.id}")

        # 成员统一登记: 任意内容类型的群消息 (文字/图片/贴纸/语音/投票/命令等)
        # 与群内回调 (点按钮/投票/竞猜) 都补记发言人档案, 只写档案不加消息数
        # (发言计数仍由 pipeline 的 track_member_message 负责, 不会翻倍)。
        # Bot 自己排除; 登记失败绝不能阻断后续处理。
        try:
            if isinstance(inner, Message):
                if (
                    chat
                    and chat.type in ("group", "supergroup")
                    and inner.from_user
                    and not inner.from_user.is_bot
                ):
                    await track_member_seen(chat.id, inner.from_user)
            elif isinstance(inner, CallbackQuery):
                if (
                    chat
                    and chat.type in ("group", "supergroup")
                    and inner.from_user
                    and not inner.from_user.is_bot
                ):
                    await track_member_seen(chat.id, inner.from_user)
        except Exception as exc:
            logger.warning("中间件补记成员失败: %s", exc)

        return await handler(event, data)


@tracker_router.my_chat_member()
async def on_bot_member_updated(event: ChatMemberUpdated) -> None:
    """
    当 Bot 被拉入群组、提升为管理员或移出群组时更新 group_settings.is_active
    """
    if event.chat.type not in ("group", "supergroup"):
        return

    new_status = event.new_chat_member.status
    is_active = new_status in ("member", "administrator", "creator", "restricted")
    await ensure_group_initialized(event.chat.id, event.chat.title or f"Group {event.chat.id}")

    async with get_db() as db:
        await db.execute(
            "UPDATE group_settings SET is_active = ?, group_title = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
            (1 if is_active else 0, event.chat.title or f"Group {event.chat.id}", event.chat.id),
        )
