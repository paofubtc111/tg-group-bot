import html
import logging
import random
from datetime import datetime, timezone
from typing import List, Optional

from aiogram import Bot, F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.points import add_points, get_user_balance
from app.database.db import get_db

logger = logging.getLogger(__name__)
router = Router(name="games_router")

# 骰子：用户点数 > 机器人点数获胜；平局退还本金
DICE_WIN_MIN_ROLL = 1
DICE_WIN_MAX_ROLL = 6


# ==================== 抽奖 ====================


def _lottery_type_label(lottery_type: str) -> str:
    return {
        "msg_count": "发言数量抽奖",
        "msg_rank": "发言排名抽奖",
        "signin_count": "签到数量抽奖",
        "invite_count": "邀请数量抽奖",
        "points_scratch": "积分刮刮乐",
        "points_timed": "积分定时抽奖",
    }.get(lottery_type or "", "抽奖")


async def _user_meets_threshold(
    group_id: int, user_id: int, lottery: dict
) -> tuple[bool, str]:
    """检查用户是否满足抽奖参与门槛, 返回 (是否满足, 不满足原因)。"""
    lottery_type = lottery.get("lottery_type") or ""
    threshold = int(lottery.get("threshold") or 0)
    if threshold <= 0:
        return True, ""
    async with get_db() as db:
        if lottery_type == "msg_count":
            row = await db.fetchrow(
                "SELECT COUNT(*) AS cnt FROM group_messages WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            count = int(row["cnt"]) if row else 0
            if count < threshold:
                return False, f"发言数量不足（需要 {threshold} 条，您当前 {count} 条）"
            return True, ""
        if lottery_type == "msg_rank":
            row = await db.fetchrow(
                """
                SELECT COUNT(*) AS cnt FROM (
                    SELECT user_id, COUNT(*) AS c FROM group_messages
                    WHERE group_id = ? GROUP BY user_id HAVING c > (
                        SELECT COUNT(*) FROM group_messages WHERE group_id = ? AND user_id = ?
                    )
                )
                """,
                (group_id, group_id, user_id),
            )
            rank = int(row["cnt"]) + 1 if row else 1
            if rank > threshold:
                return False, f"发言排名不足（需要前 {threshold} 名，您当前第 {rank} 名）"
            return True, ""
        if lottery_type == "signin_count":
            row = await db.fetchrow(
                "SELECT signin_streak FROM user_points WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            streak = int((row or {}).get("signin_streak") or 0)
            if streak < threshold:
                return False, f"连续签到天数不足（需要 {threshold} 天，您当前 {streak} 天）"
            return True, ""
        if lottery_type == "invite_count":
            row = await db.fetchrow(
                "SELECT COUNT(*) AS cnt FROM invites WHERE group_id = ? AND inviter_id = ?",
                (group_id, user_id),
            )
            count = int(row["cnt"]) if row else 0
            if count < threshold:
                return False, f"邀请人数不足（需要 {threshold} 人，您当前 {count} 人）"
            return True, ""
    return True, ""


async def _draw_lottery(bot: Bot, lottery: dict) -> str:
    """执行开奖: 从参与者中随机抽取, 按奖品份数依次分配, 公布结果并返回公告文本。"""
    lottery_id = lottery["id"]
    group_id = lottery["group_id"]
    async with get_db() as db:
        entries = await db.fetch(
            "SELECT user_id, username FROM lottery_entries WHERE lottery_id = ?",
            (lottery_id,),
        )
        prizes = await db.fetch(
            """
            SELECT * FROM lottery_prizes
            WHERE lottery_id = ? AND drawn < total ORDER BY id
            """,
            (lottery_id,),
        )
    if not entries:
        return "本次抽奖无人参与，开奖取消。"
    if not prizes:
        return "本次抽奖没有可用奖品，开奖取消。"

    random.shuffle(entries)
    winners: list[tuple[dict, dict]] = []
    entry_idx = 0
    for prize in prizes:
        remaining = int(prize["total"]) - int(prize["drawn"])
        for _ in range(remaining):
            if entry_idx >= len(entries):
                break
            winners.append((entries[entry_idx], prize))
            entry_idx += 1
        if entry_idx >= len(entries):
            break

    if not winners:
        return "本次抽奖无人中奖。"
    lines = [f"🎉 「{lottery.get('title') or '抽奖'}」开奖结果："]
    async with get_db() as db:
        for entry, prize in winners:
            await db.execute(
                """
                INSERT INTO lottery_draws (lottery_id, user_id, username, prize_name)
                VALUES (?, ?, ?, ?)
                """,
                (lottery_id, entry["user_id"], entry.get("username") or "", prize["prize_name"]),
            )
            await db.execute(
                "UPDATE lottery_prizes SET drawn = drawn + 1 WHERE id = ?",
                (prize["id"],),
            )
            who = f"@{entry.get('username')}" if entry.get("username") else f"用户 {entry['user_id']}"
            lines.append(f"🏅 {who} 获得「{prize['prize_name']}」")
        await db.execute(
            "UPDATE lotteries SET status = 'ended' WHERE id = ?", (lottery_id,)
        )
    notice = "\n".join(lines)
    try:
        await bot.send_message(group_id, notice)
    except Exception as exc:
        logger.warning("抽奖开奖公告发送失败 (lottery=%s): %s", lottery_id, exc)
    return notice


async def draw_lottery_by_id(bot: Bot, lottery_id: int) -> str:
    """供面板手动开奖 / 定时开奖调用。"""
    async with get_db() as db:
        lottery = await db.fetchrow(
            "SELECT * FROM lotteries WHERE id = ?", (lottery_id,)
        )
    if not lottery:
        return "抽奖活动不存在。"
    if lottery.get("status") == "ended":
        return "该抽奖已结束。"
    return await _draw_lottery(bot, lottery)


async def _handle_scratch(
    message: types.Message, bot: Bot, group_id: int, user: types.User, lottery: dict
) -> bool:
    """积分刮刮乐: 每次消耗积分, 立即随机开奖。"""
    cost = int(lottery.get("cost_per_draw") or 0)
    max_draws = int(lottery.get("max_draws_per_user") or 1)
    async with get_db() as db:
        plays = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM lottery_plays WHERE lottery_id = ? AND user_id = ?",
            (lottery["id"], user.id),
        )
        plays_cnt = int(plays["cnt"]) if plays else 0
    if plays_cnt >= max_draws:
        await message.reply(f"您已用完本次刮刮乐次数（每人最多 {max_draws} 次）。")
        return True
    if cost > 0:
        balance = await get_user_balance(group_id, user.id)
        if balance < cost:
            await message.reply(f"积分不足：刮一次需要 {cost} 积分，您当前只有 {balance} 积分。")
            return True
        await add_points(group_id, user.id, -cost, f"刮刮乐：{lottery.get('title') or ''}")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO lottery_plays (lottery_id, user_id) VALUES (?, ?)",
            (lottery["id"], user.id),
        )
        prizes = await db.fetch(
            "SELECT * FROM lottery_prizes WHERE lottery_id = ? AND drawn < total ORDER BY id",
            (lottery["id"],),
        )
    # 50% 概率谢谢参与, 否则从剩余奖品中随机一个
    if not prizes or random.random() < 0.5:
        await message.reply("🎲 谢谢参与，再接再厉！")
        return True
    prize = random.choice(prizes)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO lottery_draws (lottery_id, user_id, username, prize_name)
            VALUES (?, ?, ?, ?)
            """,
            (lottery["id"], user.id, user.username or user.first_name or "", prize["prize_name"]),
        )
        await db.execute(
            "UPDATE lottery_prizes SET drawn = drawn + 1 WHERE id = ?",
            (prize["id"],),
        )
    await message.reply(f"🎉 恭喜中奖！您刮到了「{prize['prize_name']}」！")
    return True


async def handle_lottery_commands(message: types.Message, bot: Bot) -> bool:
    """抽奖指令: 参与关键字 / 开奖关键字 (开奖仅限群管理员)。"""
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    group_id = message.chat.id
    user = message.from_user
    if user is None or user.is_bot:
        return False

    async with get_db() as db:
        lotteries = await db.fetch(
            "SELECT * FROM lotteries WHERE group_id = ? AND status = 'active'",
            (group_id,),
        )
    if not lotteries:
        return False

    for lottery in lotteries:
        keyword = (lottery.get("keyword") or "抽奖").strip()
        draw_keyword = (lottery.get("draw_keyword") or "开奖").strip()
        lottery_type = lottery.get("lottery_type") or ""

        if text == keyword:
            if lottery_type == "points_scratch":
                return await _handle_scratch(message, bot, group_id, user, lottery)
            ok, reason = await _user_meets_threshold(group_id, user.id, lottery)
            if not ok:
                await message.reply(f"❌ 参与失败：{reason}。")
                return True
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO lottery_entries (lottery_id, user_id, username)
                    VALUES (?, ?, ?)
                    ON CONFLICT(lottery_id, user_id) DO NOTHING
                    """,
                    (lottery["id"], user.id, user.username or user.first_name or ""),
                )
            await message.reply(
                f"✅ 已成功参与「{lottery.get('title') or '抽奖'}」，"
                f"发送「{draw_keyword}」开奖，祝您好运！"
            )
            return True

        if text == draw_keyword:
            # 开奖仅限群管理员 (普通成员发送开奖关键字不触发)
            try:
                member = await bot.get_chat_member(group_id, user.id)
                if member.status not in ("administrator", "creator"):
                    await message.reply("只有群管理员可以开奖。")
                    return True
            except Exception:
                pass
            notice = await _draw_lottery(bot, lottery)
            if "取消" not in notice and "无人中奖" not in notice:
                return True
            await message.reply(notice)
            return True
    return False


async def draw_due_timed_lotteries(bot: Bot) -> None:
    """后台任务: 到期的积分定时抽奖自动开奖。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with get_db() as db:
        rows = await db.fetch(
            """
            SELECT * FROM lotteries
            WHERE status = 'active' AND lottery_type = 'points_timed'
              AND end_at IS NOT NULL AND end_at != '' AND end_at <= ?
            """,
            (now,),
        )
    for row in rows:
        try:
            await _draw_lottery(bot, row)
        except Exception as exc:
            logger.warning("定时抽奖自动开奖失败 (lottery=%s): %s", row["id"], exc)


# ==================== 积分竞猜 ====================


async def post_guess_message(bot: Bot, group_id: int, guess: dict) -> Optional[int]:
    """发布竞猜到群里 (带选项按钮), 返回消息 ID。"""
    async with get_db() as db:
        options = await db.fetch(
            "SELECT * FROM guess_options WHERE guess_id = ? ORDER BY id", (guess["id"],)
        )
    if not options:
        return None
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{o['option_text']}"
                + (f" (赔率 {o['odds']})" if guess.get("odds_mode") == "odds" else ""),
                callback_data=f"guess:bet:{guess['id']}:{o['id']}",
            )
        ]
        for o in options
    ]
    text = (
        f"🎲 <b>积分竞猜</b>\n\n{html.escape(guess.get('question') or guess.get('title') or '')}\n"
        f"每次下注 {guess.get('cost_per_bet') or 0} 积分，点击下方选项参与。"
    )
    try:
        sent = await bot.send_message(
            group_id,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
        async with get_db() as db:
            await db.execute(
                "UPDATE guesses SET posted_message_id = ? WHERE id = ?",
                (sent.message_id, guess["id"]),
            )
        return sent.message_id
    except Exception as exc:
        logger.warning("发布竞猜失败 (guess=%s): %s", guess["id"], exc)
        return None


@router.callback_query(F.data.startswith("guess:bet:"))
async def on_guess_bet_callback(query: types.CallbackQuery, bot: Bot) -> None:
    """用户点击竞猜选项: 扣除下注积分并记录。"""
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("无效的竞猜数据", show_alert=True)
        return
    try:
        guess_id = int(parts[2])
        option_id = int(parts[3])
    except ValueError:
        await query.answer("无效的竞猜数据", show_alert=True)
        return
    user = query.from_user
    async with get_db() as db:
        guess = await db.fetchrow("SELECT * FROM guesses WHERE id = ?", (guess_id,))
    if not guess or guess.get("status") != "active":
        await query.answer("该竞猜已结束。", show_alert=True)
        return
    async with get_db() as db:
        option = await db.fetchrow(
            "SELECT * FROM guess_options WHERE id = ? AND guess_id = ?",
            (option_id, guess_id),
        )
    if not option:
        await query.answer("选项不存在。", show_alert=True)
        return
    cost = int(guess.get("cost_per_bet") or 0)
    if cost > 0:
        balance = await get_user_balance(query.message.chat.id, user.id)
        if balance < cost:
            await query.answer(f"积分不足，下注需要 {cost} 积分。", show_alert=True)
            return
        await add_points(
            query.message.chat.id, user.id, -cost, f"竞猜下注：{guess.get('title') or ''}"
        )
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO guess_bets (guess_id, user_id, username, option_id, amount)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guess_id, user.id, user.username or user.first_name or "", option_id, cost),
        )
    await query.answer(f"✅ 已下注「{option['option_text']}」，消耗 {cost} 积分。", show_alert=False)


async def close_guess_and_payout(
    bot: Bot, guess_id: int, winning_option_id: int
) -> str:
    """结算竞猜: 奖品模式按奖池/固定奖金派发, 赔率模式按赔率派发。"""
    async with get_db() as db:
        guess = await db.fetchrow("SELECT * FROM guesses WHERE id = ?", (guess_id,))
    if not guess:
        return "竞猜不存在。"
    if guess.get("status") == "closed":
        return "该竞猜已结算。"
    async with get_db() as db:
        option = await db.fetchrow(
            "SELECT * FROM guess_options WHERE id = ? AND guess_id = ?",
            (winning_option_id, guess_id),
        )
        bets = await db.fetch(
            "SELECT * FROM guess_bets WHERE guess_id = ? AND option_id = ?",
            (guess_id, winning_option_id),
        )
    if not option:
        return "中奖选项不存在。"
    odds_mode = guess.get("odds_mode") or "prize"
    total_paid = 0
    winners = len(bets)
    async with get_db() as db:
        for bet in bets:
            if odds_mode == "odds":
                payout = int(round(int(bet["amount"]) * float(option.get("odds") or 2.0)))
            else:
                fixed = int(guess.get("prize_amount") or 0)
                if fixed > 0:
                    payout = fixed
                else:
                    pool = int(guess.get("prize_pool") or 0)
                    payout = pool // winners if winners else 0
            if payout > 0:
                await add_points(
                    guess["group_id"], bet["user_id"], payout,
                    f"竞猜获胜：{guess.get('title') or ''}",
                )
                total_paid += payout
            await db.execute(
                "UPDATE guess_bets SET paid_out = ? WHERE id = ?",
                (payout, bet["id"]),
            )
        await db.execute(
            "UPDATE guesses SET status = 'closed', winning_option_id = ? WHERE id = ?",
            (winning_option_id, guess_id),
        )
    notice = (
        f"🎲 竞猜「{guess.get('title') or ''}」已开奖，"
        f"中奖选项：「{option['option_text']}」，共 {winners} 人获胜，"
        f"总派发 {total_paid} 积分。"
    )
    try:
        await bot.send_message(guess["group_id"], notice)
    except Exception as exc:
        logger.warning("竞猜开奖公告发送失败 (guess=%s): %s", guess_id, exc)
    return notice


# ==================== 积分骰子 ====================


async def handle_dice_command(message: types.Message, bot: Bot) -> bool:
    """骰子游戏: 用户发送「骰子」参与, 点数大于机器人则按倍数赔付。"""
    if message.chat.type not in ("group", "supergroup"):
        return False
    text = (message.text or "").strip()
    if text != "骰子":
        return False
    group_id = message.chat.id
    user = message.from_user
    if user is None or user.is_bot:
        return False
    async with get_db() as db:
        cfg = await db.fetchrow(
            "SELECT * FROM dice_settings WHERE group_id = ?", (group_id,)
        )
    if not cfg or not cfg.get("enabled"):
        return False
    cost = int(cfg.get("cost_per_round") or 0)
    multiplier = float(cfg.get("payout_multiplier") or 2.0)
    if cost > 0:
        balance = await get_user_balance(group_id, user.id)
        if balance < cost:
            await message.reply(f"积分不足：玩一次骰子需要 {cost} 积分，您当前只有 {balance} 积分。")
            return True
        await add_points(group_id, user.id, -cost, "骰子游戏下注")
    user_roll = random.randint(DICE_WIN_MIN_ROLL, DICE_WIN_MAX_ROLL)
    bot_roll = random.randint(DICE_WIN_MIN_ROLL, DICE_WIN_MAX_ROLL)
    if user_roll > bot_roll:
        result = "win"
        payout = int(round(cost * multiplier))
        if payout > 0:
            await add_points(group_id, user.id, payout, "骰子游戏获胜")
        reply = (
            f"🎲 骰子结果：你 {user_roll} 点 vs 机器人 {bot_roll} 点，"
            f"你赢了！获得 {payout} 积分。"
        )
    elif user_roll == bot_roll:
        result = "draw"
        payout = cost
        if payout > 0:
            await add_points(group_id, user.id, payout, "骰子游戏平局返还")
        reply = (
            f"🎲 骰子结果：你 {user_roll} 点 vs 机器人 {bot_roll} 点，"
            f"平局，本金 {payout} 积分已返还。"
        )
    else:
        result = "lose"
        payout = 0
        reply = (
            f"🎲 骰子结果：你 {user_roll} 点 vs 机器人 {bot_roll} 点，"
            f"你输了，{cost} 积分已扣除。"
        )
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO dice_records (group_id, user_id, username, cost, user_roll, bot_roll, result, payout)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                user.id,
                user.username or user.first_name or "",
                cost,
                user_roll,
                bot_roll,
                result,
                payout,
            ),
        )
    await message.reply(reply)
    return True
