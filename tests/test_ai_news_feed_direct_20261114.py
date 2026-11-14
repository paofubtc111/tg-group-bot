# -*- coding: utf-8 -*-
"""Muse 动态直接发突发测试（SQLite 本地）：不用关键词、不用打分。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    ~/workspace/.venv-bot-test/bin/python tests/test_ai_news_feed_direct_20261114.py

背景（pao 2026-11-14 22:16、22:20）：Nex-N2.6、cryptovps 这类动态标题
过不了 RSS 的关键词门，快讯收不到。pao 先选“跳过关键词、直接打分”，
随后改成“直接发，不用打分不用选关键词”。规则：
- 动态（source 以 Muse 开头）直接进突发候选，不走关键词门、不打分；
- 仍受每轮每群 2 条限流，超出的留到下一轮，不丢；
- RSS 仍走关键词 + 打分（>= 4 才发），不受影响。
"""
import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news_feed_direct.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database.db import close_db, init_db, get_db
from app.bot.handlers import ai_news

CST = ZoneInfo("Asia/Shanghai")


class FakeBot:
    """只记录 send_message，不发真消息。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return None


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


async def fresh_db():
    await close_db()
    if os.path.exists("/tmp/test_ai_news_feed_direct.db"):
        os.remove("/tmp/test_ai_news_feed_direct.db")
    await init_db()
    await ai_news.ensure_default_settings()
    async with get_db() as db:
        await db.execute("DELETE FROM ai_news_items")
        await db.execute("DELETE FROM ai_news_sends")


async def seed(source, title, link):
    now = datetime.now(CST).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, title, link, now, now),
        )


async def fresh_count(source):
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS c FROM ai_news_items "
            "WHERE source = ? AND sent_breaking = 0",
            (source,),
        )
    return int((row or {}).get("c") or 0)


def counting_score(calls, value):
    async def fake(*args, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return value

    return fake


async def test_feed_no_keyword_no_score():
    """动态不过关键词门、打分给低分，照样直接发，且根本不调打分。"""
    await fresh_db()
    await seed("Muse动态", "Nex-N2.6 在 OSWorld 拿到 83.0 分", "https://example.com/nex")
    bot = FakeBot()
    calls = {}
    with patch.object(ai_news, "_score_importance",
                      side_effect=counting_score(calls, 2)):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("动态无关键词也发了 1 条", len(bot.sent) == 1)
    ok &= check("发的是 Nex 标题",
                bot.sent and "Nex-N2.6" in bot.sent[0]["text"])
    ok &= check("打分一次都没调", calls.get("n", 0) == 0)
    return ok


async def test_rss_still_gated():
    """同标题走 RSS：关键词门不过，再高分也不发，打分也不调。"""
    await fresh_db()
    await seed("TechCrunch AI", "Nex-N2.6 在 OSWorld 拿到 83.0 分", "https://example.com/nex-rss")
    bot = FakeBot()
    calls = {}
    with patch.object(ai_news, "_score_importance",
                      side_effect=counting_score(calls, 5)):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("RSS 无关键词的不发", len(bot.sent) == 0)
    ok &= check("打分没被调（门先拦了）", calls.get("n", 0) == 0)
    return ok


async def test_feed_ollama_down():
    """Ollama 全挂：动态不靠打分，照样直接发。"""
    await fresh_db()
    await seed("Muse动态", "cryptovps 上线加密货币 VPS，49 个区域五步部署",
               "https://example.com/cryptovps")
    bot = FakeBot()
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("Ollama 挂了动态照发 1 条", len(bot.sent) == 1)
    ok &= check("发的是 cryptovps",
                bot.sent and "cryptovps" in bot.sent[0]["text"])
    return ok


async def test_feed_cycle_cap():
    """动态每轮每群最多 2 条：第 3 条留到下一轮，不丢。"""
    await fresh_db()
    for i in range(1, 4):
        await seed("Muse动态", f"动态闲聊 {i}", f"https://example.com/chat{i}")
    bot = FakeBot()
    with patch.object(ai_news, "_score_importance",
                      side_effect=counting_score({}, 2)):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("第一轮只发 2 条", len(bot.sent) == 2)
    ok &= check("第 3 条留在库里", await fresh_count("Muse动态") == 1)
    with patch.object(ai_news, "_score_importance",
                      side_effect=counting_score({}, 2)):
        await ai_news.check_breaking_news(bot)
    ok &= check("第二轮补发第 3 条", len(bot.sent) == 3)
    ok &= check("发完库里没剩", await fresh_count("Muse动态") == 0)
    return ok


async def main():
    results = []
    results.append(await test_feed_no_keyword_no_score())
    results.append(await test_rss_still_gated())
    results.append(await test_feed_ollama_down())
    results.append(await test_feed_cycle_cap())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
