import asyncio
import html
import json
import logging
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, User

from app.database.db import get_db

logger = logging.getLogger(__name__)


async def trigger_welcome(bot: Bot, chat_id: int, user: User) -> None:
    """
    解析消息模板、替换插值变量、发送欢迎并设置 TTL 定时销毁
    """
    async with get_db() as db:
        cfg = await db.fetchrow(
            "SELECT * FROM group_welcome_settings WHERE group_id = ?", (chat_id,)
        )
        if not cfg or not cfg["enabled"] or not cfg["active_template_id"]:
            return

        tpl = await db.fetchrow(
            "SELECT * FROM message_templates WHERE template_id = ?",
            (cfg["active_template_id"],),
        )
        if not tpl:
            return

        # 1. 防爆屏清理：若配置开启，撤回上一条欢迎语
        if cfg["clean_previous"] and cfg["last_welcome_message_id"]:
            try:
                await bot.delete_message(chat_id, cfg["last_welcome_message_id"])
            except Exception:
                pass

        # 2. 占位符变量替换（对用户昵称和群名进行 HTML 转义，防止破坏 HTML 解析）
        try:
            chat_info = await bot.get_chat(chat_id)
            group_title = html.escape(chat_info.title or "本群")
        except Exception:
            group_title = "本群"

        safe_fullname = html.escape(user.full_name or "新成员")
        mention_html = f'<a href="tg://user?id={user.id}">{safe_fullname}</a>'
        username_display = f"@{html.escape(user.username)}" if user.username else mention_html

        text = (
            tpl["content"]
            .replace("{username}", username_display)
            .replace("{fullname}", safe_fullname)
            .replace("{mention}", mention_html)
            .replace("{user_id}", str(user.id))
            .replace("{group_title}", group_title)
        )

        # 3. 组装内联按钮
        reply_markup = None
        if tpl["buttons_json"]:
            try:
                btn_rows = json.loads(tpl["buttons_json"])
                keyboard = []
                for row in btn_rows:
                    if isinstance(row, list):
                        kb_row = [
                            InlineKeyboardButton(text=str(b["text"]), url=str(b["url"]))
                            for b in row
                            if isinstance(b, dict) and b.get("text") and b.get("url")
                        ]
                        if kb_row:
                            keyboard.append(kb_row)
                if keyboard:
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            except Exception as exc:
                logger.warning("解析欢迎模板按钮 JSON 失败: %s", exc)

        # 4. 发送欢迎消息 (支持纯文本/图片/动图)
        sent_msg = None
        try:
            media_type = (tpl.get("media_type") or "none").lower()
            media_source = (tpl.get("media_source") or "").strip()
            if media_type == "photo" and media_source:
                sent_msg = await bot.send_photo(
                    chat_id,
                    photo=media_source,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            elif media_type == "animation" and media_source:
                sent_msg = await bot.send_animation(
                    chat_id,
                    animation=media_source,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            else:
                sent_msg = await bot.send_message(
                    chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        except Exception as exc:
            logger.error("发送入群欢迎消息失败 (group=%s): %s", chat_id, exc)
            return

        if not sent_msg:
            return

        # 5. 更新上一条消息 ID
        await db.execute(
            "UPDATE group_welcome_settings SET last_welcome_message_id = ? WHERE group_id = ?",
            (sent_msg.message_id, chat_id),
        )

        # 6. 注册 TTL 定时自动撤回任务
        delete_ttl = int(cfg.get("delete_ttl") or 0)
        if delete_ttl > 0:
            asyncio.create_task(auto_delete_msg(bot, chat_id, sent_msg.message_id, delete_ttl))


async def auto_delete_msg(bot: Bot, chat_id: int, message_id: int, ttl: int) -> None:
    await asyncio.sleep(ttl)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
