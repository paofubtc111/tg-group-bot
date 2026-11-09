import asyncio
import html
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

from aiogram import Bot, F, Router
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from app.bot.handlers.group_features import (
    enforce_force_subscribe,
    track_member_join,
)
from app.bot.handlers.welcome import trigger_welcome
from app.database.db import ensure_group_initialized, get_db, record_audit_log

logger = logging.getLogger(__name__)
router = Router(name="captcha_router")

# 维护活跃验证协程任务，便于验证成功时立即取消倒计时: (group_id, user_id) -> asyncio.Task
_active_tasks: Dict[Tuple[int, int], asyncio.Task] = {}

RESTRICTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_invite_users=False,
)

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

EMOJI_POOL = [
    ("🚀", "火箭"),
    ("🛡️", "盾牌"),
    ("💎", "钻石"),
    ("🍀", "四叶草"),
    ("🔥", "火焰"),
    ("🎯", "靶心"),
    ("⚡", "闪电"),
    ("🌈", "彩虹"),
]


def _build_captcha_challenge(
    captcha_type: str, group_id: int, user_id: int
) -> Tuple[str, str, InlineKeyboardMarkup]:
    """
    根据验证码类型生成题目描述、正确答案以及 InlineKeyboardMarkup
    Callback Data 统一格式: cap:{group_id}:{user_id}:{answer}
    """
    captcha_type = (captcha_type or "math").lower()

    if captcha_type == "click":
        expected = "human"
        question_desc = "请点击下方按钮确认您是真人用户："
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ 点击验证我是人类",
                        callback_data=f"cap:{group_id}:{user_id}:human",
                    )
                ]
            ]
        )
        return question_desc, expected, kb

    if captcha_type == "choice":
        candidates = random.sample(EMOJI_POOL, 6)
        target_emoji, target_name = random.choice(candidates)
        expected = target_emoji
        question_desc = f"请在下方按钮中找出并点击 <b>【{target_emoji} {target_name}】</b> 图标："
        row1 = [
            InlineKeyboardButton(
                text=emo, callback_data=f"cap:{group_id}:{user_id}:{emo}"
            )
            for emo, _ in candidates[:3]
        ]
        row2 = [
            InlineKeyboardButton(
                text=emo, callback_data=f"cap:{group_id}:{user_id}:{emo}"
            )
            for emo, _ in candidates[3:]
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[row1, row2])
        return question_desc, expected, kb

    # 默认 math 四则算术模式
    a = random.randint(2, 25)
    b = random.randint(2, 20)
    op = random.choice(["+", "-"])
    if op == "-" and a < b:
        a, b = b, a
    correct = a + b if op == "+" else a - b
    expected = str(correct)

    options = {correct}
    while len(options) < 4:
        offset = random.choice([-5, -3, -2, -1, 1, 2, 3, 5, 10])
        cand = max(0, correct + offset)
        options.add(cand)

    shuffled = list(options)
    random.shuffle(shuffled)
    question_desc = f"请计算算术题：<b>{a} {op} {b} = ?</b>"
    buttons = [
        InlineKeyboardButton(
            text=str(val), callback_data=f"cap:{group_id}:{user_id}:{val}"
        )
        for val in shuffled
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[buttons])
    return question_desc, expected, kb


async def handle_new_member_join(bot: Bot, chat_id: int, chat_title: str, user: User) -> None:
    """
    处理单个新成员入群的核心流程
    """
    if user.is_bot:
        return

    await ensure_group_initialized(chat_id, chat_title or f"Group {chat_id}")

    # 登记入群成员档案
    await track_member_join(chat_id, user)

    # 强制订阅关卡: 未订阅指定频道者禁言并提示, 跳过后续验证/欢迎流程,
    # 待其点击「我已加入」并通过核查后重新进入本流程。
    if await enforce_force_subscribe(bot, chat_id, user):
        return

    async with get_db() as db:
        emergency = await db.fetchrow(
            "SELECT * FROM group_emergency_settings WHERE group_id = ?", (chat_id,)
        )
        if emergency and emergency.get("raid_protection"):
            try:
                await bot.ban_chat_member(chat_id, user.id)
                await bot.unban_chat_member(chat_id, user.id)
                await record_audit_log(
                    chat_id,
                    0,
                    "SystemGuard",
                    "raid_kick",
                    f"防爆群模式生效，自动移出新入群用户 {user.full_name} ({user.id})",
                )
            except Exception as exc:
                logger.warning("防爆群移出用户失败: %s", exc)
            return

        cfg = await db.fetchrow(
            "SELECT * FROM group_captcha_settings WHERE group_id = ?", (chat_id,)
        )

    # 1. 若未开启入群验证，直接触发欢迎语
    if not cfg or not cfg["enabled"]:
        await trigger_welcome(bot, chat_id, user)
        return

    # 2. 立即剥夺新成员发言与媒体发送权限
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user.id,
            permissions=RESTRICTED_PERMISSIONS,
        )
    except Exception as exc:
        logger.warning("限制新成员权限失败 (请确认 Bot 具备管理员封禁权限): %s", exc)

    # 3. 生成验证题并发送验证卡片
    timeout_sec = int(cfg.get("timeout") or 60)
    captcha_type = cfg.get("captcha_type") or "math"
    question_desc, expected_answer, reply_markup = _build_captcha_challenge(
        captcha_type, chat_id, user.id
    )

    safe_name = html.escape(user.full_name or "新成员")
    mention = f'<a href="tg://user?id={user.id}">{safe_name}</a>'
    prompt_text = (
        f"🛡️ <b>入群安全验证</b>\n\n"
        f"欢迎 {mention} 加入本群！为防止广告机器人，请在 <b>{timeout_sec} 秒</b> 内完成以下验证：\n"
        f"👉 {question_desc}\n\n"
        f"<i>⚠️ 超时未完成验证将被系统自动{'移出群聊' if cfg.get('action_on_fail') == 'kick' else '封禁拉黑'}。</i>"
    )

    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=prompt_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("发送入群验证卡片失败: %s", exc)
        return

    expire_at = (datetime.now(timezone.utc) + timedelta(seconds=timeout_sec)).isoformat()

    async with get_db() as db:
        # 清理该用户可能残留的旧验证记录
        old_rec = await db.fetchrow(
            "SELECT message_id FROM pending_verifications WHERE group_id = ? AND user_id = ?",
            (chat_id, user.id),
        )
        if old_rec:
            try:
                await bot.delete_message(chat_id, old_rec["message_id"])
            except Exception:
                pass

        await db.execute(
            """
            INSERT INTO pending_verifications (group_id, user_id, message_id, expected_answer, expire_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                message_id = excluded.message_id,
                expected_answer = excluded.expected_answer,
                expire_at = excluded.expire_at
            """,
            (chat_id, user.id, sent_msg.message_id, expected_answer, expire_at),
        )

    # 4. 派发后台异步超时监控协程
    key = (chat_id, user.id)
    if key in _active_tasks and not _active_tasks[key].done():
        _active_tasks[key].cancel()

    _active_tasks[key] = asyncio.create_task(
        _verification_timeout_worker(
            bot=bot,
            chat_id=chat_id,
            user_id=user.id,
            message_id=sent_msg.message_id,
            delay=float(timeout_sec),
        )
    )


async def _verification_timeout_worker(
    bot: Bot, chat_id: int, user_id: int, message_id: int, delay: float
) -> None:
    """
    超时未完成验证监控协程
    """
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        async with get_db() as db:
            pending = await db.fetchrow(
                "SELECT * FROM pending_verifications WHERE group_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            if not pending:
                return

            cfg = await db.fetchrow(
                "SELECT * FROM group_captcha_settings WHERE group_id = ?", (chat_id,)
            )
            await db.execute(
                "DELETE FROM pending_verifications WHERE group_id = ? AND user_id = ?",
                (chat_id, user_id),
            )

        action_on_fail = (cfg.get("action_on_fail") if cfg else "kick") or "kick"
        auto_delete = bool(cfg.get("auto_delete_prompt", True)) if cfg else True

        try:
            if action_on_fail == "ban":
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            else:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception as exc:
            logger.warning("超时处罚用户失败 (group=%s, user=%s): %s", chat_id, user_id, exc)

        if auto_delete:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

        await record_audit_log(
            chat_id,
            0,
            "SystemCaptcha",
            f"captcha_timeout_{action_on_fail}",
            f"用户 {user_id} 入群验证超时，已执行 {action_on_fail}",
        )
    except asyncio.CancelledError:
        return
    finally:
        _active_tasks.pop((chat_id, user_id), None)


@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join_chat_member(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in ("group", "supergroup"):
        return
    # 邀请关系记录 (邀请链接创建者即邀请人；无链接时 inviter 为空)
    try:
        from app.bot.handlers.group_features import record_invite

        inviter_id = None
        link = event.invite_link
        if link and link.creator:
            inviter_id = link.creator.id
        await record_invite(
            event.chat.id, inviter_id, event.new_chat_member.user.id
        )
    except Exception:
        pass
    await handle_new_member_join(
        bot, event.chat.id, event.chat.title or "", event.new_chat_member.user
    )


@router.message(F.new_chat_members)
async def on_new_chat_members_message(message: Message, bot: Bot) -> None:
    """
    兼容部分群组通过 service message 推送 new_chat_members 事件
    """
    if message.chat.type not in ("group", "supergroup"):
        return
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        async with get_db() as db:
            already_pending = await db.fetchrow(
                "SELECT id FROM pending_verifications WHERE group_id = ? AND user_id = ?",
                (message.chat.id, member.id),
            )
        if not already_pending:
            await handle_new_member_join(
                bot, message.chat.id, message.chat.title or "", member
            )


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha_callback(query: CallbackQuery, bot: Bot) -> None:
    """
    处理用户点击验证按钮回调
    """
    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        await query.answer("无效的验证数据", show_alert=True)
        return

    _, group_id_str, target_uid_str, chosen_answer = parts
    group_id = int(group_id_str)
    target_user_id = int(target_uid_str)

    # 防篡改校验：非本人点击直接弹窗拦截
    if query.from_user.id != target_user_id:
        await query.answer("这不是你的验证题！", show_alert=True)
        return

    async with get_db() as db:
        pending = await db.fetchrow(
            "SELECT * FROM pending_verifications WHERE group_id = ? AND user_id = ?",
            (group_id, target_user_id),
        )
        if not pending:
            await query.answer("该验证已过期或已完成！", show_alert=True)
            return

        cfg = await db.fetchrow(
            "SELECT * FROM group_captcha_settings WHERE group_id = ?", (group_id,)
        )

        if chosen_answer != pending["expected_answer"]:
            await query.answer("❌ 验证答案不正确，请重试！", show_alert=True)
            return

        # 验证通过：移除待验证记录
        await db.execute(
            "DELETE FROM pending_verifications WHERE group_id = ? AND user_id = ?",
            (group_id, target_user_id),
        )

    # 取消后台超时监控协程
    task = _active_tasks.pop((group_id, target_user_id), None)
    if task and not task.done():
        task.cancel()

    await query.answer("✅ 验证通过，欢迎入群！", show_alert=False)

    await record_audit_log(
        group_id,
        target_user_id,
        query.from_user.full_name or "新成员",
        "captcha_passed",
        f"用户 {target_user_id} 通过入群验证",
    )

    # 恢复用户正常发言权限
    try:
        await bot.restrict_chat_member(
            chat_id=group_id,
            user_id=target_user_id,
            permissions=UNRESTRICTED_PERMISSIONS,
        )
    except Exception as exc:
        logger.warning("恢复用户发言权限失败: %s", exc)

    # 自动删除验证题消息（清理群聊屏幕）
    auto_delete = bool(cfg.get("auto_delete_prompt", True)) if cfg else True
    if auto_delete:
        try:
            await bot.delete_message(chat_id=group_id, message_id=pending["message_id"])
        except Exception:
            pass

    # 触发入群欢迎语发送流程
    await trigger_welcome(bot, group_id, query.from_user)


async def recover_pending_verifications(bot: Bot) -> None:
    """
    系统重启时从 SQLite 恢复未完成的验证倒计时任务
    """
    async with get_db() as db:
        rows = await db.fetch("SELECT * FROM pending_verifications")

    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            expire_dt = datetime.fromisoformat(row["expire_at"])
            if expire_dt.tzinfo is None:
                expire_dt = expire_dt.replace(tzinfo=timezone.utc)
            remaining = max(0.0, (expire_dt - now).total_seconds())
        except Exception:
            remaining = 0.0

        key = (row["group_id"], row["user_id"])
        _active_tasks[key] = asyncio.create_task(
            _verification_timeout_worker(
                bot=bot,
                chat_id=row["group_id"],
                user_id=row["user_id"],
                message_id=row["message_id"],
                delay=remaining,
            )
        )
