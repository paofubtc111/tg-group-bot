import asyncio
import ast
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import httpx
from aiogram import Bot, F, Router, types
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.database.db import get_db, record_audit_log

logger = logging.getLogger(__name__)
router = Router(name="group_features_router")

# Asia/Shanghai 固定时区 (定时发送使用, 不依赖系统 tzdata)
CST = timezone(timedelta(hours=8))

# 强制订阅检查结果缓存: (group_id, user_id) -> (blocked, checked_at)
# 避免每条群消息都调用两次 Telegram API。
# 通过结果缓存 10 分钟；拦截结果仅缓存 60 秒, 让刚订阅完的用户再次发言时能
# 尽快重新核查通过 (主解禁路径仍是点击「我已加入」, 回调会立即清除缓存)。
_force_sub_cache: Dict[Tuple[int, int], Tuple[bool, float]] = {}
FORCE_SUB_PASS_TTL = 600.0
FORCE_SUB_BLOCKED_TTL = 60.0

UNRESTRICTED_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


# ==================== 群成员档案 ====================


async def track_member_message(chat_id: int, user: types.User) -> None:
    """群消息上报成员活跃: 更新用户名/昵称、消息数 +1、最近发言时间。"""
    if user is None or user.is_bot:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO group_members (group_id, user_id, username, first_name, message_count, last_seen, joined_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    message_count = group_members.message_count + 1,
                    last_seen = excluded.last_seen
                """,
                (chat_id, user.id, user.username or "", user.first_name or "", now, now),
            )
    except Exception as exc:
        logger.warning("记录成员活跃失败 (group=%s, user=%s): %s", chat_id, user.id, exc)


async def track_member_join(chat_id: int, user: types.User) -> None:
    """新成员入群登记档案 (不增加消息数, 不覆盖已有的入群时间)。"""
    if user is None or user.is_bot:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO group_members (group_id, user_id, username, first_name, message_count, last_seen, joined_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = excluded.last_seen
                """,
                (chat_id, user.id, user.username or "", user.first_name or "", now, now),
            )
    except Exception as exc:
        logger.warning("登记入群成员失败 (group=%s, user=%s): %s", chat_id, user.id, exc)


async def track_member_seen(chat_id: int, user: types.User) -> None:
    """仅补记成员档案 (不增加消息数): 中间件对所有群 update 调用。

    语义与 track_member_join 相同 (INSERT ... ON CONFLICT DO UPDATE,
    只写档案不碰 message_count), 避免中间件登记导致发言计数翻倍。
    """
    await track_member_join(chat_id, user)


# ==================== 群消息归档 ====================


async def log_group_message(
    chat_id: int, message_id: int, user: types.User, text: str
) -> None:
    """归档群文本消息, 每群仅保留最新 500 条。"""
    if user is None:
        return
    try:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO group_messages (group_id, message_id, user_id, username, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    message_id,
                    user.id,
                    user.username or user.first_name or "",
                    (text or "")[:2000],
                ),
            )
            await db.execute(
                """
                DELETE FROM group_messages
                WHERE group_id = ? AND id NOT IN (
                    SELECT id FROM group_messages WHERE group_id = ? ORDER BY id DESC LIMIT 500
                )
                """,
                (chat_id, chat_id),
            )
    except Exception as exc:
        logger.warning("归档群消息失败 (group=%s, msg=%s): %s", chat_id, message_id, exc)


# ==================== 关键词自动回复 ====================


async def check_keyword_reply(message: types.Message, bot: Bot) -> bool:
    """
    匹配本群启用的关键词自动回复, 命中则发送回复并返回 True (调用方应停止后续 LLM 处理)。
    """
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or message.caption or "").strip()
    if not text:
        return False

    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM keyword_replies WHERE group_id = ? AND enabled = 1",
            (message.chat.id,),
        )

    for r in rows:
        keyword = (r.get("keyword") or "").strip()
        if not keyword:
            continue
        match_type = (r.get("match_type") or "contains").lower()
        if match_type == "exact":
            matched = text.lower() == keyword.lower()
        else:
            matched = keyword.lower() in text.lower()
        if not matched:
            continue
        reply_text = r.get("reply_text") or ""
        if not reply_text:
            return True
        try:
            await message.reply(reply_text)
        except Exception:
            # 含 HTML 特殊字符时退回纯文本发送
            try:
                await message.reply(html.escape(reply_text), parse_mode="HTML")
            except Exception as exc:
                logger.warning("发送关键词自动回复失败: %s", exc)
        return True
    return False


# ==================== 强制订阅 ====================


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def enforce_force_subscribe(
    bot: Bot,
    chat_id: int,
    user: types.User,
    source_message: Optional[types.Message] = None,
) -> bool:
    """
    强制订阅检查。用户未订阅指定频道时禁言并发送订阅提示, 返回 True (调用方应停止后续处理)。
    未配置、频道不可达或 Bot 不是频道管理员时静默放行, 绝不破坏正常聊天。
    """
    if bot is None or user is None or user.is_bot:
        return False

    cache_key = (chat_id, user.id)
    cached = _force_sub_cache.get(cache_key)
    if cached:
        ttl = FORCE_SUB_BLOCKED_TTL if cached[0] else FORCE_SUB_PASS_TTL
        if (time.time() - cached[1]) < ttl:
            return cached[0]

    blocked = False
    try:
        async with get_db() as db:
            cfg = await db.fetchrow(
                "SELECT * FROM group_force_subscribe WHERE group_id = ?", (chat_id,)
            )
        if not cfg or not cfg.get("enabled"):
            return False
        channel = (cfg.get("channel") or "").strip()
        if not channel:
            return False

        # 群管理员豁免
        if await _is_group_admin(bot, chat_id, user.id):
            return False

        # 检查频道订阅状态
        try:
            sub = await bot.get_chat_member(channel, user.id)
            if sub.status in ("member", "administrator", "creator", "restricted"):
                return False
        except Exception as exc:
            logger.warning(
                "强制订阅频道检查失败 (channel=%s, 请确认 Bot 已加入该频道并具备管理员权限): %s",
                channel,
                exc,
            )
            return False

        # 未订阅: 禁言 + 删除触发消息 + 发送订阅提示
        blocked = True
        try:
            await bot.restrict_chat_member(
                chat_id,
                user.id,
                permissions=ChatPermissions(can_send_messages=False),
            )
        except Exception as exc:
            logger.warning("强制订阅禁言失败: %s", exc)

        if source_message is not None:
            try:
                await bot.delete_message(chat_id, source_message.message_id)
            except Exception:
                pass

        prompt = (cfg.get("prompt_text") or "").strip() or "请先加入指定频道后再发言。"
        channel_name = channel.lstrip("@")
        buttons: list[list[InlineKeyboardButton]] = []
        if channel_name and not channel.startswith("-"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="📢 加入频道",
                        url=f"https://t.me/{channel_name}",
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ 我已加入",
                    callback_data=f"sub:joined:{chat_id}:{user.id}",
                )
            ]
        )
        try:
            sent = await bot.send_message(
                chat_id,
                f"🔒 <b>强制订阅</b>\n\n{html.escape(prompt)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML",
            )
            # 提示 5 分钟后自动清理, 避免刷屏
            asyncio.create_task(_auto_delete_prompt(bot, chat_id, sent.message_id, 300))
        except Exception as exc:
            logger.warning("发送强制订阅提示失败: %s", exc)
    finally:
        _force_sub_cache[cache_key] = (blocked, time.time())
    return blocked


async def _auto_delete_prompt(bot: Bot, chat_id: int, message_id: int, ttl: int) -> None:
    await asyncio.sleep(ttl)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


@router.callback_query(F.data.startswith("sub:joined:"))
async def on_subscribed_callback(query: types.CallbackQuery, bot: Bot) -> None:
    """用户点击「我已加入」: 重新核查订阅, 通过则解禁并继续走入群验证/欢迎流程。"""
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("无效的回调数据", show_alert=True)
        return
    try:
        group_id = int(parts[2])
        target_uid = int(parts[3])
    except ValueError:
        await query.answer("无效的回调数据", show_alert=True)
        return

    if query.from_user.id != target_uid:
        await query.answer("这不是你的订阅提示！", show_alert=True)
        return

    # 清除缓存, 强制重新核查
    _force_sub_cache.pop((group_id, target_uid), None)
    blocked = await enforce_force_subscribe(bot, group_id, query.from_user)
    if blocked:
        await query.answer("还未加入频道，请先点击上方按钮加入。", show_alert=True)
        return

    await query.answer("✅ 已确认订阅，欢迎发言！", show_alert=False)
    try:
        await bot.restrict_chat_member(
            group_id,
            target_uid,
            permissions=UNRESTRICTED_PERMISSIONS,
        )
    except Exception as exc:
        logger.warning("强制订阅通过后解禁失败: %s", exc)

    if query.message:
        try:
            await bot.delete_message(group_id, query.message.message_id)
        except Exception:
            pass

    # 重新进入入群流程 (验证或欢迎)。此处延迟导入避免与 captcha 模块循环引用。
    from app.bot.handlers.captcha import handle_new_member_join

    chat_title = ""
    if query.message and query.message.chat:
        chat_title = query.message.chat.title or ""
    await handle_new_member_join(bot, group_id, chat_title, query.from_user)


# ==================== 定时发送 ====================


async def _send_due_scheduled_messages(bot: Bot) -> None:
    """检查并发送到期的定时消息 (每天 HH:MM, Asia/Shanghai, 每条每天最多发一次)。"""
    now = datetime.now(CST)
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    async with get_db() as db:
        rows = await db.fetch(
            """
            SELECT * FROM scheduled_messages
            WHERE enabled = 1 AND send_time <= ?
              AND (last_sent_date IS NULL OR last_sent_date != ?)
            """,
            (hhmm, today),
        )
    for row in rows:
        try:
            await bot.send_message(row["group_id"], row["content"])
            async with get_db() as db:
                await db.execute(
                    "UPDATE scheduled_messages SET last_sent_date = ? WHERE id = ?",
                    (today, row["id"]),
                )
            logger.info(
                "定时消息已发送 (id=%s, group=%s, time=%s)",
                row["id"],
                row["group_id"],
                row["send_time"],
            )
        except Exception as exc:
            logger.warning("定时消息发送失败 (id=%s): %s", row["id"], exc)


async def scheduled_sender_loop(bot: Bot) -> None:
    """后台定时任务主循环, 每 60 秒检查一次。Bot 为 None 时由调用方跳过启动。"""
    logger.info("定时任务主循环已启动 (每 60 秒检查一次, 时区 Asia/Shanghai)")
    while True:
        try:
            await _send_due_scheduled_messages(bot)
            await _post_due_rotating_ads(bot)
            await _apply_scheduled_lockdowns(bot)
            await _expire_keyword_locks(bot)
            await _draw_due_timed_lotteries_safe(bot)
            await _prune_point_logs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("定时任务检查异常: %s", exc)
        await asyncio.sleep(60)


# ==================== 轮播广告 ====================


async def _post_due_rotating_ads(bot: Bot) -> None:
    """按间隔轮播广告 (每群轮询发送, 展示时长到期后自动删除)。"""
    now = datetime.now(CST)
    async with get_db() as db:
        settings_rows = await db.fetch(
            "SELECT * FROM rotating_ads_settings WHERE enabled = 1"
        )
    for cfg in settings_rows:
        group_id = cfg["group_id"]
        interval = int(cfg.get("interval_minutes") or 60)
        if interval <= 0:
            continue
        last_post = cfg.get("last_post_at") or ""
        if last_post:
            try:
                last_dt = datetime.fromisoformat(last_post)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=CST)
                if (now - last_dt).total_seconds() < interval * 60:
                    continue
            except ValueError:
                pass
        async with get_db() as db:
            ads = await db.fetch(
                "SELECT * FROM rotating_ads WHERE group_id = ? AND enabled = 1 ORDER BY id",
                (group_id,),
            )
        if not ads:
            continue
        next_idx = (int(cfg.get("last_ad_index") or -1) + 1) % len(ads)
        ad = ads[next_idx]
        try:
            text = (ad.get("text") or "").strip()
            link = (ad.get("link_url") or "").strip()
            image = (ad.get("image_url") or "").strip()
            caption = text or "📢 广告"
            buttons = None
            if link:
                buttons = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🔗 查看详情", url=link)]
                    ]
                )
            if image:
                sent = await bot.send_photo(
                    group_id, image, caption=caption, reply_markup=buttons
                )
            else:
                sent = await bot.send_message(
                    group_id, caption, reply_markup=buttons
                )
            async with get_db() as db:
                await db.execute(
                    """
                    UPDATE rotating_ads_settings
                    SET last_ad_index = ?, last_post_at = ? WHERE group_id = ?
                    """,
                    (next_idx, now.isoformat(timespec="seconds"), group_id),
                )
            display_seconds = int(cfg.get("display_seconds") or 0)
            if display_seconds > 0:
                asyncio.create_task(
                    _auto_delete_prompt(bot, group_id, sent.message_id, display_seconds)
                )
        except Exception as exc:
            logger.warning("轮播广告发送失败 (group=%s): %s", group_id, exc)


# ==================== 定时关群 / 话术关群 ====================


async def _lock_group(
    bot: Bot,
    group_id: int,
    source: str,
    locked_until: Optional[str] = None,
    schedule_id: Optional[int] = None,
) -> None:
    """全员禁言并记录锁状态 (source 区分定时/话术, 与紧急模式互不干扰)。"""
    try:
        await bot.set_chat_permissions(
            group_id, permissions=ChatPermissions(can_send_messages=False)
        )
    except Exception as exc:
        logger.warning("锁群设置权限失败 (group=%s): %s", group_id, exc)
        return
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_lock_state (group_id, locked, source, locked_until, schedule_id)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                locked = 1, source = excluded.source,
                locked_until = excluded.locked_until,
                schedule_id = excluded.schedule_id
            """,
            (group_id, source, locked_until, schedule_id),
        )


async def _unlock_group(bot: Bot, group_id: int, source: str) -> None:
    """仅当锁状态来源匹配时才解除全员禁言 (不干扰紧急模式的锁)。

    解锁失败时保持 DB 状态不变 (下个 tick 重试), 并记审计日志,
    避免“权限掉了”和“没锁”无法区分。
    """
    async with get_db() as db:
        state = await db.fetchrow(
            "SELECT * FROM group_lock_state WHERE group_id = ?", (group_id,)
        )
    if not state or not state.get("locked") or state.get("source") != source:
        return
    try:
        await bot.set_chat_permissions(
            group_id, permissions=UNRESTRICTED_PERMISSIONS
        )
    except Exception as exc:
        logger.error("解群锁设置权限失败 (group=%s, source=%s): %s", group_id, source, exc)
        try:
            await record_audit_log(
                group_id,
                0,
                "AutoModerator",
                "unlock_failed",
                f"解锁失败 (source={source}): {exc}，DB 锁状态保持不变，下个循环将重试",
            )
        except Exception:
            pass
        return
    async with get_db() as db:
        await db.execute(
            "UPDATE group_lock_state SET locked = 0, locked_until = NULL WHERE group_id = ?",
            (group_id,),
        )


def _in_time_window(now_hhmm: str, start: str, end: str) -> bool:
    """判断当前 HH:MM 是否落在 [start, end) 窗口内 (支持跨午夜)。"""
    if start <= end:
        return start <= now_hhmm < end
    return now_hhmm >= start or now_hhmm < end


async def _apply_scheduled_lockdowns(bot: Bot) -> None:
    """定时关群: 按群聚合决策, 每群每 tick 只做一次锁/解。

    多规则同群不再逐条独立判定 (避免一条在窗内、一条在窗外时先锁后解)。
    有效的话术锁 (source='keyword' 且未到期) 不被定时循环覆写。
    窗口外且 source='schedule' 的锁每 tick 重试解锁, 直到成功。
    """
    now_hhmm = datetime.now(CST).strftime("%H:%M")
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM scheduled_lockdowns WHERE enabled = 1"
        )
    by_group: Dict[int, list] = {}
    for row in rows:
        by_group.setdefault(row["group_id"], []).append(row)
    for group_id, rules in by_group.items():
        try:
            in_window = False
            window_rule = None
            for r in rules:
                start = (r.get("start_time") or "00:00")[:5]
                end = (r.get("end_time") or "00:00")[:5]
                if _in_time_window(now_hhmm, start, end):
                    in_window = True
                    window_rule = r
                    break
            async with get_db() as db:
                state = await db.fetchrow(
                    "SELECT * FROM group_lock_state WHERE group_id = ?",
                    (group_id,),
                )
            if in_window:
                if state and state.get("locked") and state.get("source") == "keyword":
                    locked_until = state.get("locked_until") or ""
                    if locked_until and locked_until > datetime.now(CST).isoformat(
                        timespec="seconds"
                    ):
                        continue
                if not state or not state.get("locked") or state.get("source") != "schedule":
                    await _lock_group(bot, group_id, "schedule", schedule_id=window_rule["id"])
                    logger.info(
                        "定时关群已锁群 (group=%s, %s-%s)",
                        group_id,
                        window_rule.get("start_time"),
                        window_rule.get("end_time"),
                    )
            else:
                if state and state.get("locked") and state.get("source") == "schedule":
                    await _unlock_group(bot, group_id, "schedule")
        except Exception as exc:
            logger.warning("定时关群执行失败 (group=%s): %s", group_id, exc)


async def _expire_keyword_locks(bot: Bot) -> None:
    """话术关群到期自动解群锁。

    到期时若定时规则正处窗口内, 把锁移交给 'schedule' (locked 保持 1,
    locked_until 清空) 而不是解锁, 避免定时窗口内出现 <=60 秒空档。
    解锁失败时不发“已解锁”公告, 下个 tick 重试。
    """
    now = datetime.now(CST).isoformat(timespec="seconds")
    now_hhmm = datetime.now(CST).strftime("%H:%M")
    async with get_db() as db:
        rows = await db.fetch(
            """
            SELECT group_id FROM group_lock_state
            WHERE locked = 1 AND source = 'keyword'
              AND locked_until IS NOT NULL AND locked_until <= ?
            """,
            (now,),
        )
    for row in rows:
        group_id = row["group_id"]
        try:
            async with get_db() as db:
                sched = await db.fetch(
                    "SELECT * FROM scheduled_lockdowns WHERE group_id = ? AND enabled = 1",
                    (group_id,),
                )
            handover = None
            for r in sched:
                start = (r.get("start_time") or "00:00")[:5]
                end = (r.get("end_time") or "00:00")[:5]
                if _in_time_window(now_hhmm, start, end):
                    handover = r
                    break
            if handover is not None:
                async with get_db() as db:
                    await db.execute(
                        """
                        UPDATE group_lock_state
                        SET source = 'schedule', locked_until = NULL, schedule_id = ?
                        WHERE group_id = ? AND source = 'keyword'
                        """,
                        (handover["id"], group_id),
                    )
                logger.info(
                    "话术锁到期, 定时窗口内, 锁已移交 schedule (group=%s)", group_id
                )
                continue
            await _unlock_group(bot, group_id, "keyword")
            async with get_db() as db:
                state = await db.fetchrow(
                    "SELECT locked FROM group_lock_state WHERE group_id = ?",
                    (group_id,),
                )
            if not state or not state.get("locked"):
                await bot.send_message(group_id, "🔓 话术关群时间到，群组已自动解锁。")
            else:
                logger.warning("话术锁解锁失败, 下个循环重试 (group=%s)", group_id)
        except Exception as exc:
            logger.warning("话术关群自动解锁失败 (group=%s): %s", group_id, exc)


async def check_lockdown_keyword(message: types.Message, bot: Bot) -> bool:
    """话术关群: 群消息命中关群关键词时锁群并公告。

    管理员默认豁免触发; 仅当该关键词 allow_admin_trigger=1 时管理员也可触发。
    匹配不区分大小写, 读 text 或 caption。
    """
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or message.caption or "").strip()
    if not text or not message.from_user:
        return False
    try:
        async with get_db() as db:
            rows = await db.fetch(
                "SELECT * FROM lockdown_keywords WHERE group_id = ? AND enabled = 1",
                (message.chat.id,),
            )
        if not rows:
            return False
        hit = None
        for r in rows:
            keyword = (r.get("keyword") or "").strip()
            if keyword and keyword.lower() in text.lower():
                hit = r
                break
        if not hit:
            return False
        if await _is_group_admin(bot, message.chat.id, message.from_user.id):
            if not hit.get("allow_admin_trigger"):
                logger.debug(
                    "话术关群: 管理员触发已豁免 (group=%s, keyword=%s)",
                    message.chat.id,
                    hit.get("keyword"),
                )
                return False
        minutes = int(hit.get("lock_minutes") or 30)
        until = (datetime.now(CST) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        await _lock_group(bot, message.chat.id, "keyword", locked_until=until)
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        await bot.send_message(
            message.chat.id,
            f"🔒 检测到关群话术「{html.escape((hit.get('keyword') or '').strip())}」，"
            f"群组已锁定 {minutes} 分钟。",
        )
        return True
    except Exception as exc:
        logger.warning("话术关群检查失败: %s", exc)
        return False


async def _draw_due_timed_lotteries_safe(bot: Bot) -> None:
    """到期的积分定时抽奖自动开奖 (延迟导入避免循环引用)。"""
    try:
        from app.bot.handlers.games import draw_due_timed_lotteries

        await draw_due_timed_lotteries(bot)
    except Exception as exc:
        logger.warning("定时抽奖检查失败: %s", exc)


async def _prune_point_logs() -> None:
    """积分日志仅保留 30 天。"""
    try:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM point_logs WHERE created_at < datetime('now', '-30 days')"
            )
    except Exception as exc:
        logger.warning("积分日志清理失败: %s", exc)


# ==================== 退群拉黑 / 邀请记录 / 服务消息清理 ====================


async def record_invite(
    group_id: int, inviter_id: Optional[int], invited_id: int
) -> None:
    """记录一次邀请关系 (邀请人未知时 inviter_id 为 NULL, 排行榜只统计已知邀请人)。"""
    try:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO invites (group_id, inviter_id, invited_id)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id, invited_id) DO NOTHING
                """,
                (group_id, inviter_id, invited_id),
            )
    except Exception as exc:
        logger.warning("记录邀请关系失败: %s", exc)


async def _get_misc_settings(group_id: int) -> dict:
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_misc_settings WHERE group_id = ?", (group_id,)
        )
    if row:
        return row
    return {
        "auto_delete_join": 1,
        "auto_delete_leave": 1,
        "auto_delete_pinned_notice": 1,
        "auto_delete_promotion": 0,
        "cancel_channel_pin": 0,
        "calculator": 0,
        "auto_check_u": 0,
    }


@router.chat_member()
async def on_member_status_changed(event: types.ChatMemberUpdated, bot: Bot) -> None:
    """
    成员状态变更: 入群记录邀请关系 (含邀请链接创建者), 退群按开关拉黑。
    入群验证流程仍由 captcha 的 chat_member 处理器负责, 此处不重复触发。
    """
    if event.chat.type not in ("group", "supergroup"):
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    user = event.new_chat_member.user
    if user is None or user.is_bot:
        return
    group_id = event.chat.id

    was_member = old_status in ("member", "administrator", "creator", "restricted")
    is_member = new_status in ("member", "administrator", "creator", "restricted")
    if is_member and not was_member:
        inviter_id = None
        try:
            link = event.invite_link
            if link and link.creator:
                inviter_id = link.creator.id
        except Exception:
            pass
        await record_invite(group_id, inviter_id, user.id)
        # 入群补记成员档案 (与 captcha 入群登记互为备份)
        await track_member_join(group_id, user)
        return

    if not is_member and was_member and new_status in ("left", "kicked"):
        try:
            await record_audit_log(
                group_id,
                user.id,
                user.full_name or user.username or str(user.id),
                "member_leave",
                f"成员退群: {user.full_name} ({user.id})",
            )
        except Exception:
            pass
        try:
            async with get_db() as db:
                row = await db.fetchrow(
                    "SELECT enabled FROM group_leave_ban WHERE group_id = ?",
                    (group_id,),
                )
            if row and row.get("enabled"):
                await bot.ban_chat_member(group_id, user.id)
                logger.info("退群拉黑已执行 (group=%s, user=%s)", group_id, user.id)
        except Exception as exc:
            logger.warning("退群拉黑失败 (group=%s, user=%s): %s", group_id, user.id, exc)


@router.message(F.new_chat_members)
async def on_join_service_message(message: types.Message, bot: Bot) -> None:
    """进群服务消息: 按其他设置自动删除 (入群验证流程由 captcha 处理)。"""
    if message.chat.type not in ("group", "supergroup"):
        return
    misc = await _get_misc_settings(message.chat.id)
    if misc.get("auto_delete_join"):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass


@router.message(F.left_chat_member)
async def on_leave_service_message(message: types.Message, bot: Bot) -> None:
    """退群服务消息: 按其他设置自动删除；退群拉黑 (无邀请链接信息时不记录邀请)。"""
    if message.chat.type not in ("group", "supergroup"):
        return
    misc = await _get_misc_settings(message.chat.id)
    if misc.get("auto_delete_leave"):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
    user = message.left_chat_member
    if user is not None and not user.is_bot:
        try:
            async with get_db() as db:
                row = await db.fetchrow(
                    "SELECT enabled FROM group_leave_ban WHERE group_id = ?",
                    (message.chat.id,),
                )
            if row and row.get("enabled"):
                await bot.ban_chat_member(message.chat.id, user.id)
        except Exception as exc:
            logger.warning("退群拉黑失败: %s", exc)


@router.message(F.pinned_message)
async def on_pinned_service_message(message: types.Message, bot: Bot) -> None:
    """置顶提示服务消息: 按其他设置自动删除。"""
    if message.chat.type not in ("group", "supergroup"):
        return
    misc = await _get_misc_settings(message.chat.id)
    if misc.get("auto_delete_pinned_notice"):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass


# ==================== 用户改名监控 ====================


async def check_rename_watch_with_bot(
    bot: Bot, chat_id: int, user: types.User
) -> None:
    """改名监控 (带 Bot 发送公告版)。"""
    if bot is None or user is None or user.is_bot:
        return
    try:
        async with get_db() as db:
            watch = await db.fetchrow(
                "SELECT enabled FROM group_rename_watch WHERE group_id = ?", (chat_id,)
            )
            if not watch or not watch.get("enabled"):
                return
            row = await db.fetchrow(
                "SELECT username, first_name, message_count FROM group_members WHERE group_id = ? AND user_id = ?",
                (chat_id, user.id),
            )
        if not row:
            return
        old_username = row.get("username") or ""
        old_name = row.get("first_name") or ""
        new_username = user.username or ""
        new_name = user.first_name or ""
        changes = []
        if old_name and new_name and old_name != new_name:
            changes.append(f"昵称「{old_name}」→「{new_name}」")
        if old_username != new_username:
            changes.append(
                f"用户名 @{old_username or '无'} → @{new_username or '无'}"
            )
        if not changes:
            return
        if int(row.get("message_count") or 0) <= 0 and not old_name:
            return
        who = f"@{new_username}" if new_username else html.escape(new_name or str(user.id))
        await bot.send_message(
            chat_id,
            f"👀 改名监控：{who} 修改了{'、'.join(changes)}。",
        )
    except Exception as exc:
        logger.warning("改名监控公告失败: %s", exc)


# ==================== 群底部按钮 ====================


async def check_bottom_buttons(message: types.Message, bot: Bot) -> bool:
    """群底部按钮: 用户发送「广告」时弹出已启用的自定义按钮 (读 text 或 caption)。"""
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or message.caption or "").strip()
    if text != "广告":
        return False
    async with get_db() as db:
        buttons = await db.fetch(
            "SELECT * FROM bottom_buttons WHERE group_id = ? AND enabled = 1 ORDER BY id",
            (message.chat.id,),
        )
    if not buttons:
        return False
    logger.debug(
        "底部按钮命中 (group=%s, 按钮 %d 个)", message.chat.id, len(buttons)
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=b["button_text"], callback_data=f"bbtn:{b['id']}"
                )
            ]
            for b in buttons
        ]
    )
    try:
        await message.reply("👇 点击下方按钮查看内容：", reply_markup=keyboard)
    except Exception as exc:
        logger.error("发送底部按钮失败 (group=%s): %s", message.chat.id, exc)
        return False
    return True


@router.callback_query(F.data.startswith("bbtn:"))
async def on_bottom_button_callback(query: types.CallbackQuery, bot: Bot) -> None:
    """用户点击底部按钮: 发送该按钮对应的自定义文字。"""
    parts = (query.data or "").split(":")
    if len(parts) != 2:
        await query.answer("无效的按钮数据", show_alert=True)
        return
    try:
        button_id = int(parts[1])
    except ValueError:
        await query.answer("无效的按钮数据", show_alert=True)
        return
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM bottom_buttons WHERE id = ?", (button_id,)
        )
    if not row or not row.get("enabled"):
        await query.answer("该按钮已停用。", show_alert=True)
        return
    chat_id = query.message.chat.id if query.message else None
    if chat_id is None or row.get("group_id") != chat_id:
        await query.answer("该按钮不属于当前群组。", show_alert=True)
        return
    reply_text = (row.get("reply_text") or "").strip()
    if not reply_text:
        await query.answer("该按钮暂无内容。", show_alert=True)
        return
    await query.answer()
    try:
        await bot.send_message(chat_id, reply_text)
    except Exception as exc:
        logger.warning("发送底部按钮内容失败: %s", exc)


# ==================== 计算器 ====================


_CALC_ALLOWED_CHARS = re.compile(r"^[\d\s+\-*/().%]+$")
_CALC_HAS_OPERATOR = re.compile(r"[\+\-\*/%]")

_ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.FloorDiv,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


def _safe_eval_arithmetic(expr: str) -> Optional[float]:
    """安全计算四则运算表达式 (仅允许数字与 +-*/().% 运算符, 拒绝一切其他语法)。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            return None
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (int, float)
        ):
            return None
    try:
        result = eval(  # noqa: S307 - 表达式已由 AST 白名单严格校验
            compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {}
        )
    except Exception:
        return None
    if not isinstance(result, (int, float)) or isinstance(result, bool):
        return None
    return result


async def check_calculator(message: types.Message, bot: Bot) -> bool:
    """计算器: 群内发送纯算术表达式时返回结果 (需在其他设置中开启)。"""
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or "").strip()
    if not text or len(text) > 64:
        return False
    if not _CALC_ALLOWED_CHARS.match(text):
        return False
    if not _CALC_HAS_OPERATOR.search(text):
        return False
    if not any(ch.isdigit() for ch in text):
        return False
    misc = await _get_misc_settings(message.chat.id)
    if not misc.get("calculator"):
        return False
    result = _safe_eval_arithmetic(text)
    if result is None:
        return False
    if isinstance(result, float):
        result_text = f"{result:.6f}".rstrip("0").rstrip(".")
    else:
        result_text = str(result)
    try:
        await message.reply(f"🧮 {text} = {result_text}")
    except Exception as exc:
        logger.warning("计算器回复失败: %s", exc)
    return True


# ==================== 自动查U (TRC20) ====================


TRC20_ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID_API = "https://api.trongrid.io/v1/accounts/{address}"
OKX_TICKER_API = "https://www.okx.com/api/v5/market/ticker"


async def check_trc20_balance(message: types.Message, bot: Bot) -> bool:
    """自动查U: 群内发送 TRC20 地址时查询 USDT/TRX 余额与价值 (无 Key 公开接口)。"""
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or "").strip()
    if not TRC20_ADDRESS_RE.match(text):
        return False
    misc = await _get_misc_settings(message.chat.id)
    if not misc.get("auto_check_u"):
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(TRONGRID_API.format(address=text))
            resp.raise_for_status()
            data = resp.json().get("data") or []
            if not data:
                await message.reply("未查询到该地址的链上数据，请确认地址正确。")
                return True
            account = data[0]
            trx_balance = int(account.get("balance") or 0) / 1_000_000
            usdt_balance = 0.0
            # Trongrid 的 trc20 是 [{合约地址: 余额字符串}] (键即合约地址),
            # 不是 [{tokenId, balance}]; 两种结构都兼容读取。
            for token in account.get("trc20") or []:
                if isinstance(token, dict):
                    if USDT_TRC20_CONTRACT in token:
                        usdt_balance = int(token[USDT_TRC20_CONTRACT] or 0) / 1_000_000
                        break
                    if token.get("tokenId") == USDT_TRC20_CONTRACT:
                        usdt_balance = int(token.get("balance") or 0) / 1_000_000
                        break
            trx_price = 0.0
            try:
                ticker = await client.get(
                    OKX_TICKER_API, params={"instId": "TRX-USDT"}
                )
                ticker.raise_for_status()
                tick_data = ticker.json().get("data") or []
                if tick_data:
                    trx_price = float(tick_data[0].get("last") or 0)
            except Exception:
                pass
        trx_value = trx_balance * trx_price
        usdt_value = usdt_balance  # 1 USDT ≈ 1 美元
        await message.reply(
            f"🔍 地址 {text}\n"
            f"TRX：{trx_balance:.4f}（≈ ${trx_value:.2f}）\n"
            f"USDT：{usdt_balance:.4f}（≈ ${usdt_value:.2f}）\n"
            f"合计：≈ ${trx_value + usdt_value:.2f}"
        )
    except Exception as exc:
        logger.warning("自动查U失败: %s", exc)
        try:
            await message.reply("查询暂不可用，请稍后再试。")
        except Exception:
            pass
    return True
