# -*- coding: utf-8 -*-
"""今日运势：消息带“运势”就抽签。

规则（pao 2026-11-13 定）：
- 群聊与私聊都触发：消息文本/标题含“运势”两个字即可，不用 @机器人。
- 默认四档，各 25%：大吉 / 吉 / 凶 / 大凶，话术可配。
- 同一个用户一天全局一种：按北京时间 00:00 分界，当天重复问返回同一结果
  （档位 + 话术都固定），不另加提示语。
- 各群可在面板里开关、增减档位、改权重与话术；私聊用默认配置。
- 纯本地随机，不走 Ollama，不花积分。
"""
import json
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import types

from app.database.db import get_db

logger = logging.getLogger(__name__)

CST = ZoneInfo("Asia/Shanghai")

DEFAULT_FORTUNE_TIERS = [
    {"name": "大吉", "weight": 25, "lines": ["总裁您今天吉星高照，运势腾达，天降横财。"]},
    {"name": "吉", "weight": 25, "lines": ["今天手气不错，把握住小机会，小赚一笔。"]},
    {"name": "凶", "weight": 25, "lines": ["今天水逆，注意小人，别被害了，稳住别浪。"]},
    {"name": "大凶", "weight": 25, "lines": ["总裁您今日诸事不宜，适合躺平，钱包捂紧，明天再战。"]},
]


def today_str() -> str:
    """北京时间今天的日期（YYYY-MM-DD），一天一次的分界线。"""
    return datetime.now(CST).strftime("%Y-%m-%d")


def _clean_tiers(raw) -> list:
    """清洗档位配置（面板/库里来的都可能脏）：非法档位丢弃，全非法回默认。"""
    if not isinstance(raw, list) or not raw:
        return [dict(t, lines=list(t["lines"])) for t in DEFAULT_FORTUNE_TIERS]
    tiers = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        lines = [str(x).strip() for x in (t.get("lines") or []) if str(x).strip()]
        try:
            weight = float(t.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0
        if not name or not lines or weight <= 0:
            continue
        tiers.append({"name": name, "weight": weight, "lines": lines})
    return tiers or [dict(t, lines=list(t["lines"])) for t in DEFAULT_FORTUNE_TIERS]


async def get_fortune_config(group_id: int | None):
    """返回 (enabled, tiers)。私聊 group_id=None 时用默认配置、默认开启。"""
    if group_id is None:
        return True, _clean_tiers(DEFAULT_FORTUNE_TIERS)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_fortune_settings WHERE group_id = ?", (group_id,)
        )
    if not row:
        return True, _clean_tiers(DEFAULT_FORTUNE_TIERS)
    try:
        raw = json.loads(row["tiers_json"]) if row["tiers_json"] else None
    except (TypeError, ValueError):
        raw = None
    return bool(row["enabled"]), _clean_tiers(raw)


async def draw_fortune(user_id: int, tiers: list) -> tuple:
    """抽一次：同一 user 同一天返回同一结果（档位 + 话术都固定）。"""
    date_label = today_str()
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT tier_name, line_text FROM user_daily_fortune "
            "WHERE user_id = ? AND fortune_date = ?",
            (user_id, date_label),
        )
        if row:
            return row["tier_name"], row["line_text"]
        tier = random.choices(tiers, weights=[t["weight"] for t in tiers], k=1)[0]
        line = random.choice(tier["lines"])
        await db.execute(
            "INSERT OR IGNORE INTO user_daily_fortune "
            "(user_id, fortune_date, tier_name, line_text) VALUES (?, ?, ?, ?)",
            (user_id, date_label, tier["name"], line),
        )
        # 并发重复抽时以库里已存的那条为准，保证同一天全局唯一
        row = await db.fetchrow(
            "SELECT tier_name, line_text FROM user_daily_fortune "
            "WHERE user_id = ? AND fortune_date = ?",
            (user_id, date_label),
        )
        if row:
            return row["tier_name"], row["line_text"]
        return tier["name"], line


async def check_daily_fortune(message: types.Message, bot) -> bool:
    """消息带“运势”就抽签回复，命中返回 True（管道结束，不再走关键词/AI）。"""
    text = (message.text or message.caption or "")
    if "运势" not in text or not message.from_user:
        return False
    is_group = message.chat.type in ("group", "supergroup")
    enabled, tiers = await get_fortune_config(message.chat.id if is_group else None)
    if not enabled:
        return False
    tier_name, line = await draw_fortune(message.from_user.id, tiers)
    user = message.from_user
    display = f"@{user.username}" if user.username else (user.first_name or "朋友")
    try:
        await message.reply(f"🎋 {display} 今日运势：{tier_name}\n{line}")
    except Exception as exc:
        logger.warning("发送今日运势失败: %s", exc)
        return False
    return True
