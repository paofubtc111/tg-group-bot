import asyncio
import html
import logging
import re
from typing import Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from app.config import settings
from app.database.db import get_db, record_audit_log

logger = logging.getLogger(__name__)
router = Router(name="moderation_router")

TG_LINK_REGEX = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/[a-zA-Z0-9_+@-]+", re.IGNORECASE)
GENERAL_URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@router.message(Command("start", "settings", "panel"))
async def cmd_open_panel(message: types.Message) -> None:
    """
    唤起 Telegram Mini App 可视化群管后台或 안내消息
    """
    if message.chat.type == "private":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎛️ 打开群管可视化控制面板 (Mini App)",
                        web_app=WebAppInfo(url=settings.WEBAPP_BASE_URL),
                    )
                ]
            ]
        )
        await message.answer(
            "👋 <b>欢迎使用群组自动化管理与本地 AI 交互管家！</b>\n\n"
            "点击下方按钮即可唤起 <b>Telegram Mini App 控制面板</b>，支持：\n"
            "• 🛡️ 敏感词与外链/转发安全拦截\n"
            "• 🧩 入群人机验证（算术/图符/点选）\n"
            "• 💌 多模板欢迎语与防刷屏定时撤回\n"
            "• 🤖 本地 Ollama (qwen2.5:7b) 智能对话配置\n"
            "• ⚡ 紧急全员锁群与配置一键导入导出",
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        me = await message.bot.get_me()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚙️ 前往私聊打开群管面板",
                        url=f"https://t.me/{me.username}?start=settings",
                    )
                ]
            ]
        )
        reply = await message.reply(
            "🔒 为保护群组配置隐私，请点击下方按钮在私聊中打开 Mini App 管理面板：",
            reply_markup=kb,
        )
        asyncio.create_task(auto_delete_warning(message.bot, message.chat.id, reply.message_id, 15))


@router.message(Command("admin", "report"))
async def cmd_call_admins(message: types.Message, bot: Bot) -> None:
    """
    群成员使用 /admin 或 /report 呼叫群组管理员
    """
    if message.chat.type not in ("group", "supergroup"):
        return

    async with get_db() as db:
        emg = await db.fetchrow(
            "SELECT * FROM group_emergency_settings WHERE group_id = ?", (message.chat.id,)
        )

    if emg and not emg.get("enable_admin_call", True):
        return

    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        admin_mentions = [
            f'<a href="tg://user?id={adm.user.id}">@{html.escape(adm.user.username or adm.user.first_name)}</a>'
            for adm in admins
            if not adm.user.is_bot
        ]
        mention_text = " ".join(admin_mentions[:8]) if admin_mentions else "群组管理员"
        await message.reply(
            f"🚨 <b>呼叫群组管理员</b>\n{mention_text}\n"
            f"来自成员 {html.escape(message.from_user.full_name)} 的举报或求助，请及时查看。",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("呼叫管理员失败: %s", exc)


async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def inspect_and_moderate_message(message: types.Message, bot: Bot) -> bool:
    """
    检查群消息是否触发紧急锁群、外链/转发拦截或敏感词规则。
    若违规并已被拦截处理则返回 True，正常放行则返回 False。
    """
    chat_id = message.chat.id
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return False

    # 1. 管理员豁免白名单
    if await is_user_admin(bot, chat_id, message.from_user.id):
        return False

    content_text = (message.text or message.caption or "").strip()

    async with get_db() as db:
        emg = await db.fetchrow(
            "SELECT * FROM group_emergency_settings WHERE group_id = ?", (chat_id,)
        )
        sec = await db.fetchrow(
            "SELECT * FROM group_moderation_settings WHERE group_id = ?", (chat_id,)
        )
        rules = await db.fetch(
            "SELECT * FROM group_sensitive_rules WHERE group_id = ?", (chat_id,)
        )

    # 2. 紧急锁群模式拦截
    if emg and emg.get("lockdown_mode"):
        try:
            await bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        return True

    has_violation = False
    violation_reason = "违规内容"
    target_rule: Optional[dict] = None

    # 3. 转发与外链检测
    if sec:
        if sec.get("block_forwards") and (
            getattr(message, "forward_origin", None) is not None
            or getattr(message, "forward_from", None) is not None
            or getattr(message, "forward_from_chat", None) is not None
        ):
            has_violation = True
            violation_reason = "禁止转发外部频道或他人消息"

        if not has_violation and sec.get("block_tg_links") and content_text:
            if TG_LINK_REGEX.search(content_text):
                has_violation = True
                violation_reason = "禁止发送 Telegram 群组/频道链接"

        if not has_violation and sec.get("block_all_urls"):
            entities = (message.entities or []) + (message.caption_entities or [])
            for ent in entities:
                if ent.type in ("url", "text_link"):
                    has_violation = True
                    violation_reason = "禁止发送外部网页链接"
                    break
            if not has_violation and content_text and GENERAL_URL_REGEX.search(content_text):
                has_violation = True
                violation_reason = "禁止发送外部网页链接"

    # 4. 敏感词规则扫描
    if not has_violation and rules and content_text:
        for r in rules:
            pattern = r.get("pattern") or ""
            if not pattern:
                continue
            match_type = (r.get("match_type") or "contains").lower()
            if match_type == "contains" and pattern.lower() in content_text.lower():
                has_violation = True
                target_rule = r
                violation_reason = f"命中敏感词规则"
                break
            elif match_type == "exact" and content_text.lower() == pattern.lower():
                has_violation = True
                target_rule = r
                violation_reason = f"命中精确敏感词"
                break
            elif match_type == "regex":
                try:
                    if re.search(pattern, content_text, re.IGNORECASE):
                        has_violation = True
                        target_rule = r
                        violation_reason = f"命中正则拦截规则"
                        break
                except re.error:
                    continue

    if not has_violation:
        return False

    # 5. 秒删违规原消息
    try:
        await bot.delete_message(chat_id, message.message_id)
    except Exception as exc:
        logger.warning("删除违规消息失败: %s", exc)

    # 6. 累计用户违规次数并判定阶梯处罚
    user_id = message.from_user.id
    max_warn = int(sec.get("max_warn_count") or 3) if sec else 3
    warn_action = (sec.get("warn_action") or "kick") if sec else "kick"

    async with get_db() as db:
        warn_row = await db.fetchrow(
            "SELECT warn_count FROM user_warn_records WHERE group_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        current_warns = (int(warn_row["warn_count"]) + 1) if warn_row else 1
        await db.execute(
            """
            INSERT INTO user_warn_records (group_id, user_id, warn_count, last_violation_time)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                warn_count = excluded.warn_count,
                last_violation_time = CURRENT_TIMESTAMP
            """,
            (chat_id, user_id, current_warns),
        )

    # 优先判定是否达到累计警告上限，否则执行单条规则设定的处罚动作
    action = target_rule["punish_action"] if target_rule else "delete_only"
    mute_duration = int(target_rule.get("mute_duration") or 3600) if target_rule else 3600

    if current_warns >= max_warn:
        action = warn_action
        async with get_db() as db:
            await db.execute(
                "DELETE FROM user_warn_records WHERE group_id = ? AND user_id = ?",
                (chat_id, user_id),
            )

    action_desc = "已撤回消息"
    try:
        if action == "mute":
            until_ts = int(message.date.timestamp() + mute_duration)
            await bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_ts,
            )
            action_desc = f"已禁言 {mute_duration // 60} 分钟"
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            action_desc = "已移出群聊"
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            action_desc = "已永久封禁"
    except Exception as exc:
        logger.warning("执行违规处罚失败 (action=%s): %s", action, exc)

    # 7. 发送临时告警消息并注册 TTL 自动销毁
    if not sec or sec.get("send_warning_msg", True):
        ttl = int(sec.get("warning_ttl") or 5) if sec else 5
        display_user = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else html.escape(message.from_user.first_name or "群成员")
        )
        try:
            warn_msg = await message.answer(
                f"⚠️ {display_user} 您的发言因【{violation_reason}】已被系统拦截（警告 {current_warns}/{max_warn}，{action_desc}）。",
                parse_mode="HTML",
            )
            if ttl > 0:
                asyncio.create_task(auto_delete_warning(bot, chat_id, warn_msg.message_id, ttl))
        except Exception:
            pass

    await record_audit_log(
        chat_id,
        0,
        "AutoModerator",
        f"violation_{action}",
        f"拦截用户 {message.from_user.full_name} ({user_id}): {violation_reason} -> {action_desc}",
    )
    return True


async def auto_delete_warning(bot: Bot, chat_id: int, message_id: int, ttl: int) -> None:
    await asyncio.sleep(ttl)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
