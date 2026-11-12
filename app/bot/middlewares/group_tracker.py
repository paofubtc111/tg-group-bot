import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Router
from aiogram.types import TelegramObject, Message, CallbackQuery, ChatMemberUpdated, Update

from app.bot.handlers.group_features import check_rename_watch_with_bot, track_member_seen
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

        # 改名监控 (必须在 track_member_seen 更新档案之前对比旧档案)。
        # 不能放管道里做: 中间件先跑, 会先把档案更新成当前名字, 管道再对比
        # 时旧名新名永远相等, 监控永远不触发。Bot 从 data 取, 取不到时跳过。
        # 成员统一登记: 任意内容类型的群消息 (文字/图片/贴纸/语音/投票/命令等)
        # 与群内回调 (点按钮/投票/竞猜) 都补记发言人档案, 只写档案不加消息数
        # (发言计数仍由 pipeline 的 track_member_message 负责, 不会翻倍)。
        # Bot 自己排除; 登记失败绝不能阻断后续处理。
        try:
            _user = None
            if isinstance(inner, (Message, CallbackQuery)):
                _user = inner.from_user
            if (
                _user
                and not _user.is_bot
                and chat
                and chat.type in ("group", "supergroup")
            ):
                _mw_bot = data.get("bot")
                if _mw_bot is not None:
                    await check_rename_watch_with_bot(_mw_bot, chat.id, _user)
                await track_member_seen(chat.id, _user)
        except Exception as exc:
            logger.warning("中间件补记成员失败: %s", exc)

        return await handler(event, data)


@tracker_router.my_chat_member()
async def on_bot_member_updated(event: ChatMemberUpdated) -> None:
    """
    当 Bot 被拉入群组、提升为管理员或移出群组时更新 group_settings.is_active，
    并在首次拉入 / 被踢后重新拉入时记下操作人（added_by_*）。
    操作人取自 event.from_user（Telegram API 的 from 字段）；机器人自己
    或无操作人时不记。已记过且 Bot 一直在群里时不再覆盖（例如仅提权为管理员）。
    """
    if event.chat.type not in ("group", "supergroup"):
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    is_active = new_status in ("member", "administrator", "creator", "restricted")
    title = event.chat.title or f"Group {event.chat.id}"
    await ensure_group_initialized(event.chat.id, title)

    # 是否记操作人: Bot 被拉进来, 且原来没记过, 或之前是被踢/离开状态。
    actor = event.from_user
    record_inviter = False
    if is_active and actor is not None and not actor.is_bot:
        async with get_db() as db:
            row = await db.fetchrow(
                "SELECT is_active, added_by_user_id FROM group_settings WHERE group_id = ?",
                (event.chat.id,),
            )
        record_inviter = bool(
            row is None
            or row.get("added_by_user_id") is None
            or not row.get("is_active")
            or old_status in ("left", "kicked")
        )

    async with get_db() as db:
        if record_inviter:
            await db.execute(
                "UPDATE group_settings SET is_active = ?, group_title = ?, added_by_user_id = ?, added_by_username = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (1 if is_active else 0, title, actor.id, actor.username, event.chat.id),
            )
        else:
            await db.execute(
                "UPDATE group_settings SET is_active = ?, group_title = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (1 if is_active else 0, title, event.chat.id),
            )
