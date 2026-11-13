# -*- coding: utf-8 -*-
"""手动测试早报 + 每日突发上限 测试。

跑法：
  /home/hatch/workspace/.venv-bot-test/bin/python tests/test_ai_news_testdigest_20261112.py
用 DEV_MODE 绕过 Telegram 签名；用假 Bot 拦截 send_message，不真发。
"""

import asyncio
import os
import sys
from datetime import date, timedelta

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news_td.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

GROUP = -1004496842671
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})
        return None


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def main():
    if os.path.exists("/tmp/test_ai_news_td.db"):
        os.remove("/tmp/test_ai_news_td.db")
    from app.database.db import close_db, get_db, init_db
    asyncio.run(init_db())

    # 造 3 条昨天的新闻
    async def seed():
        async with get_db() as db:
            for i, title in enumerate([
                "GPT-5 正式发布，推理能力大幅提升",
                "Meta 开源新一代大模型",
                "OpenAI 融资 100 亿美元",
            ]):
                await db.execute(
                    "INSERT INTO ai_news_items (source, title, link, published_at, fetched_at, summary) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("TechCrunch AI", title, f"https://example.com/{i}",
                     YESTERDAY, YESTERDAY, f"摘要{i}"),
                )
    asyncio.run(seed())

    from app.bot.handlers.ai_news import _max_breaking_per_day
    from app.main import app
    fake = FakeBot()
    app.state.bot = fake
    client = TestClient(app)
    ok = True

    # 1. 上限 helper
    ok &= check("上限默认 50", _max_breaking_per_day({}) == 50)
    ok &= check("上限空串默认 50", _max_breaking_per_day({"max_breaking_per_day": ""}) == 50)
    ok &= check("上限 5", _max_breaking_per_day({"max_breaking_per_day": 5}) == 5)
    ok &= check("上限 50 有效", _max_breaking_per_day({"max_breaking_per_day": 50}) == 50)
    ok &= check("上限 51 越界回默认", _max_breaking_per_day({"max_breaking_per_day": 51}) == 50)
    ok &= check("上限 0 越界回默认", _max_breaking_per_day({"max_breaking_per_day": 0}) == 50)
    ok &= check("上限 99 越界回默认", _max_breaking_per_day({"max_breaking_per_day": 99}) == 50)
    ok &= check("上限非法回默认", _max_breaking_per_day({"max_breaking_per_day": "abc"}) == 50)

    # 2. 手动测试早报：发给群，Ollama 不通所以是标题清单（fallback）
    r = client.post(f"/api/group/{GROUP}/ai-news/test-digest")
    ok &= check("测试早报 200", r.status_code == 200)
    data = r.json()
    ok &= check("测试早报 status ok", data.get("status") == "ok")
    ok &= check("测试早报 3 条新闻", data.get("items") == 3)
    ok &= check("Ollama 不通降级 fallback", data.get("fallback") is True)
    ok &= check("假 Bot 收到 1 条", len(fake.sent) == 1)
    ok &= check("发到正确的群", fake.sent[0]["chat_id"] == GROUP)
    ok &= check("早报文案含标题", "GPT-5 正式发布" in fake.sent[0]["text"])
    ok &= check("早报文案含日期头", "昨日 AI 大事件" in fake.sent[0]["text"])
    ok &= check("返回文案和发送一致", data.get("text") == fake.sent[0]["text"])

    # 3. 可重复跑（不记 last_digest_date，不影响定时早报）
    r = client.post(f"/api/group/{GROUP}/ai-news/test-digest")
    ok &= check("重复跑也 200", r.status_code == 200)
    ok &= check("重复跑又发 1 条", len(fake.sent) == 2)

    # 4. 没新闻时返回 no_news、不发
    async def clear_items():
        async with get_db() as db:
            await db.execute("DELETE FROM ai_news_items")
    asyncio.run(clear_items())
    r = client.post(f"/api/group/{GROUP}/ai-news/test-digest")
    ok &= check("没新闻返回 no_news", r.json().get("status") == "no_news")
    ok &= check("没新闻不发", len(fake.sent) == 2)

    asyncio.run(close_db())
    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
