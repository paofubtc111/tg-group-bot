import html
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, types

from app.database.db import get_db

logger = logging.getLogger(__name__)

# Asia/Shanghai 固定时区 (与定时任务保持一致)
CST = timezone(timedelta(hours=8))

POINTS_STRENGTH_STREAK_BONUS_MIN = 3  # 连续签到满 3 天起获得额外奖励


# ==================== 基础读写 ====================


async def get_point_settings(group_id: int) -> dict:
    """读取本群积分设置, 不存在则返回默认值 (不写库, 由 ensure 初始化兜底)。"""
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM point_settings WHERE group_id = ?", (group_id,)
        )
    if row:
        return row
    return {
        "group_id": group_id,
        "alias": "积分",
        "daily_max": 0,
        "decimals": 0,
        "auto_clear_inactive": 0,
        "signin_points": 10,
        "streak_bonus_points": 5,
        "enabled": 1,
    }


async def get_user_balance(group_id: int, user_id: int) -> int:
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT balance FROM user_points WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
    return int(row["balance"]) if row else 0


async def get_user_rank(group_id: int, user_id: int) -> int:
    """按积分余额排名 (并列按先达到者居前, 此处用余额>我的用户数+1 近似)。"""
    balance = await get_user_balance(group_id, user_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM user_points WHERE group_id = ? AND balance > ?",
            (group_id, balance),
        )
    return int(row["cnt"]) + 1 if row else 1


async def add_points(
    group_id: int, user_id: int, delta: int, reason: str
) -> int:
    """
    增减积分并写日志, 返回最新余额。delta 可正可负；余额不会扣成负数
    (扣减时若余额不足则返回当前余额且不写日志, 调用方自行判断)。
    """
    if delta == 0:
        return await get_user_balance(group_id, user_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT balance, total_earned FROM user_points WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        balance = int(row["balance"]) if row else 0
        total_earned = int(row["total_earned"]) if row else 0
        if delta < 0 and balance + delta < 0:
            return balance
        new_balance = balance + delta
        new_earned = total_earned + delta if delta > 0 else total_earned
        await db.execute(
            """
            INSERT INTO user_points (group_id, user_id, balance, total_earned)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                balance = excluded.balance,
                total_earned = excluded.total_earned,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, user_id, new_balance, new_earned),
        )
        await db.execute(
            """
            INSERT INTO point_logs (group_id, user_id, delta, reason)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, user_id, delta, reason),
        )
    return new_balance


async def _today_message_earned(group_id: int, user_id: int, today: str) -> int:
    """今日已获得的发言积分 (用于每日上限控制, 仅统计 reason 以'发言'开头的记录)。"""
    async with get_db() as db:
        row = await db.fetchrow(
            """
            SELECT COALESCE(SUM(delta), 0) AS s FROM point_logs
            WHERE group_id = ? AND user_id = ? AND delta > 0
              AND reason LIKE '发言%'
              AND date(created_at, '+8 hours') = ?
            """,
            (group_id, user_id, today),
        )
    return int(row["s"]) if row else 0


async def _has_first_message_today(group_id: int, user_id: int, today: str) -> bool:
    """今日是否已有首条发言积分记录。"""
    async with get_db() as db:
        row = await db.fetchrow(
            """
            SELECT id FROM point_logs
            WHERE group_id = ? AND user_id = ? AND reason = '发言（首条）'
              AND date(created_at, '+8 hours') = ? LIMIT 1
            """,
            (group_id, user_id, today),
        )
    return row is not None


# ==================== 发言积分 ====================


async def award_message_points(group_id: int, user: types.User) -> None:
    """
    按积分规则为一次群发言计分: 首条 / 每条 / 随机。
    积分系统关闭或无启用规则时直接跳过；超过每日上限的部分不再发放。
    """
    if user is None or user.is_bot:
        return
    try:
        settings = await get_point_settings(group_id)
        if not settings.get("enabled", True):
            return
        async with get_db() as db:
            rules = await db.fetch(
                """
                SELECT * FROM point_rules
                WHERE group_id = ? AND enabled = 1
                """,
                (group_id,),
            )
        if not rules:
            return

        today = datetime.now(CST).strftime("%Y-%m-%d")
        daily_max = int(settings.get("daily_max") or 0)
        earned_today = (
            await _today_message_earned(group_id, user.id, today)
            if daily_max > 0
            else 0
        )
        is_first_today = not await _has_first_message_today(
            group_id, user.id, today
        )

        for rule in rules:
            action = (rule.get("trigger_action") or "").strip()
            points = int(rule.get("points") or 0)
            if points <= 0:
                continue
            reason = ""
            if action == "first_message":
                if not is_first_today:
                    continue
                reason = "发言（首条）"
            elif action == "every_message":
                reason = "发言"
            elif action == "random_message":
                chance = int(rule.get("chance") or 100)
                if random.randint(1, 100) > max(1, min(100, chance)):
                    continue
                reason = "发言（随机）"
            else:
                continue

            if daily_max > 0 and earned_today >= daily_max:
                break
            grant = points
            if daily_max > 0:
                grant = min(points, daily_max - earned_today)
            if grant <= 0:
                continue
            await add_points(group_id, user.id, grant, reason)
            earned_today += grant
            if action == "first_message":
                is_first_today = False
    except Exception as exc:
        logger.warning("发言积分发放失败 (group=%s, user=%s): %s", group_id, user.id, exc)


# ==================== 积分指令 ====================


async def _do_signin(
    group_id: int, user: types.User, settings: dict
) -> str:
    """签到: 每日一次, 连续签到满 3 天起额外奖励。"""
    alias = settings.get("alias") or "积分"
    signin_points = int(settings.get("signin_points") or 10)
    streak_bonus = int(settings.get("streak_bonus_points") or 5)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")

    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM user_points WHERE group_id = ? AND user_id = ?",
            (group_id, user.id),
        )
    last_date = (row or {}).get("last_signin_date") or ""
    streak = int((row or {}).get("signin_streak") or 0)
    if last_date == today:
        balance = await get_user_balance(group_id, user.id)
        return (
            f"✅ 今日已签到（连续 {streak} 天），当前{alias}：{balance}。"
        )

    new_streak = streak + 1 if last_date == yesterday else 1
    grant = signin_points
    bonus_text = ""
    if new_streak >= POINTS_STRENGTH_STREAK_BONUS_MIN and streak_bonus > 0:
        grant += streak_bonus
        bonus_text = f"（含连续签到奖励 +{streak_bonus}）"
    await add_points(group_id, user.id, grant, f"签到（连续{new_streak}天）")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO user_points (group_id, user_id, last_signin_date, signin_streak)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                last_signin_date = excluded.last_signin_date,
                signin_streak = excluded.signin_streak,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, user.id, today, new_streak),
        )
    balance = await get_user_balance(group_id, user.id)
    return (
        f"✅ 签到成功！获得 {grant} {alias}{bonus_text}，"
        f"连续签到 {new_streak} 天，当前{alias}：{balance}。"
    )


async def handle_points_commands(message: types.Message, bot: Bot) -> bool:
    """
    积分指令处理。命中则回复并返回 True。
    指令: 签到 / 积分 / 积分排行 / 流水, 以及积分兑换关键词 (精确匹配)。
    """
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    group_id = message.chat.id
    user = message.from_user
    if user is None or user.is_bot:
        return False

    try:
        settings = await get_point_settings(group_id)
        if not settings.get("enabled", True):
            return False
        alias = settings.get("alias") or "积分"
        name = html.escape(user.first_name or user.username or str(user.id))

        if text == "签到":
            await message.reply(await _do_signin(group_id, user, settings))
            return True

        if text == "积分":
            balance = await get_user_balance(group_id, user.id)
            rank = await get_user_rank(group_id, user.id)
            await message.reply(
                f"您当前的{alias}为 {balance}，在本群的排名为：第 {rank} 名。"
            )
            return True

        if text == "积分排行":
            async with get_db() as db:
                rows = await db.fetch(
                    """
                    SELECT user_id, balance FROM user_points
                    WHERE group_id = ? AND balance > 0
                    ORDER BY balance DESC LIMIT 10
                    """,
                    (group_id,),
                )
            if not rows:
                await message.reply(f"本群暂无{alias}排行数据，快来发言签到赚{alias}吧！")
                return True
            lines = [f"🏆 {alias}排行榜 TOP {len(rows)}："]
            for i, r in enumerate(rows, start=1):
                lines.append(f"{i}. 用户 {r['user_id']}：{r['balance']} {alias}")
            await message.reply("\n".join(lines))
            return True

        if text == "流水":
            async with get_db() as db:
                rows = await db.fetch(
                    """
                    SELECT delta, reason, created_at FROM point_logs
                    WHERE group_id = ? AND user_id = ?
                    ORDER BY id DESC LIMIT 10
                    """,
                    (group_id, user.id),
                )
            if not rows:
                await message.reply(f"您最近暂无{alias}流水记录。")
                return True
            lines = [f"📒 最近{alias}流水："]
            for r in rows:
                sign = "+" if int(r["delta"]) > 0 else ""
                when = str(r["created_at"] or "")[5:16]
                lines.append(f"{when}  {sign}{r['delta']}  {r['reason'] or ''}")
            balance = await get_user_balance(group_id, user.id)
            lines.append(f"当前{alias}：{balance}")
            await message.reply("\n".join(lines))
            return True

        # 积分兑换关键词 (精确匹配)
        async with get_db() as db:
            redemption = await db.fetchrow(
                """
                SELECT * FROM point_redemptions
                WHERE group_id = ? AND enabled = 1 AND keyword = ?
                """,
                (group_id, text),
            )
        if redemption:
            cost = int(redemption.get("cost") or 0)
            balance = await get_user_balance(group_id, user.id)
            if balance < cost:
                await message.reply(
                    f"❌ {alias}不足：兑换「{html.escape(text)}」需要 {cost} {alias}，"
                    f"您当前只有 {balance} {alias}。"
                )
                return True
            await add_points(group_id, user.id, -cost, f"兑换：{text}")
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO point_redemption_records (group_id, user_id, redemption_id, keyword, cost)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, user.id, redemption["id"], text, cost),
                )
            desc = (redemption.get("description") or "").strip()
            new_balance = balance - cost
            await message.reply(
                f"✅ 兑换成功！「{html.escape(text)}」已兑换"
                + (f"：{html.escape(desc)}" if desc else "")
                + f"，消耗 {cost} {alias}，剩余 {alias}：{new_balance}。"
            )
            return True
    except Exception as exc:
        logger.warning("积分指令处理失败 (group=%s): %s", group_id, exc)
    return False
