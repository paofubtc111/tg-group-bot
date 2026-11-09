from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Router
from aiogram.types import TelegramObject, Message, CallbackQuery, ChatMemberUpdated

from app.database.db import get_db, ensure_group_initialized

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
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat
        elif isinstance(event, ChatMemberUpdated):
            chat = event.chat

        if chat and chat.type in ("group", "supergroup"):
            await ensure_group_initialized(chat.id, chat.title or f"Group {chat.id}")

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
