# -*- coding: utf-8 -*-
"""英文快讯标题翻译测试（SQLite 本地）：英文标题先译成中文再发。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    ~/workspace/.venv-bot-test/bin/python tests/test_ai_news_translate_20261114.py

背景（pao 2026-11-14）：02:56、06:57 两条快讯是英文标题直接发的，
英文快讯要有翻译。规则：标题不含中文字符才翻译（中文/中英混合不翻）；
翻译走 qwen2.5，失败/超时发原文，快讯不断；首次翻译失败后本轮不再调翻译。
"""
import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news_translate.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database.db import close_db, init_db, get_db
from app.bot.handlers import ai_news

CST = ZoneInfo("Asia/Shanghai")
GROUP = ai_news.DEFAULT_GROUP_ID

EN_TITLE = "OpenAI launches GPT-5 with new reasoning mode"
EN_TITLE_2 = "Google launches Gemini 4 for enterprise search"
ZH_TRANSLATION = "OpenAI 推出 GPT-5，全新推理模式"


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
    if os.path.exists("/tmp/test_ai_news_translate.db"):
        os.remove("/tmp/test_ai_news_translate.db")
    await init_db()
    await ai_news.ensure_default_settings()
    async with get_db() as db:
        await db.execute("DELETE FROM ai_news_items")
        await db.execute("DELETE FROM ai_news_sends")


async def seed(title, link):
    now = datetime.now(CST).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("TechCrunch AI", title, link, now, now),
        )


def make_fake_ollama(calls, translate_result=ZH_TRANSLATION, score_result="5"):
    """按 system_prompt 分发：打分回数字，翻译回中文译文；记调用次数。"""

    async def fake(**kwargs):
        prompt = kwargs.get("system_prompt", "")
        if "打分" in prompt:
            calls["score"] = calls.get("score", 0) + 1
            return score_result
        if "翻译" in prompt:
            calls["translate"] = calls.get("translate", 0) + 1
            return translate_result
        return None

    return fake


async def test_is_english_title():
    ok = True
    ok &= check("纯英文标题要翻译", ai_news._is_english_title(EN_TITLE) is True)
    ok &= check("中文标题不翻译",
                ai_news._is_english_title("OpenAI 正式发布 GPT-5") is False)
    ok &= check("中英混合不翻译",
                ai_news._is_english_title("GPT-5 发布：全新推理模式") is False)
    ok &= check("空标题不翻译", ai_news._is_english_title("") is False)
    return ok


async def test_english_translated():
    await fresh_db()
    await seed(EN_TITLE, "https://example.com/en1")
    bot = FakeBot()
    calls = {}
    with patch.object(ai_news, "query_ollama_chat",
                      side_effect=make_fake_ollama(calls)):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("英文快讯发了 1 条", len(bot.sent) == 1)
    text = bot.sent[0]["text"] if bot.sent else ""
    ok &= check("发的是中文译文", ZH_TRANSLATION in text)
    ok &= check("不再含英文原标题", "launches" not in text)
    ok &= check("原文链接还在", "https://example.com/en1" in text)
    ok &= check("翻译只调了一次", calls.get("translate") == 1)
    return ok


async def test_chinese_not_translated():
    await fresh_db()
    await seed("OpenAI 正式发布 GPT-5", "https://example.com/zh1")
    bot = FakeBot()
    calls = {}
    with patch.object(ai_news, "query_ollama_chat",
                      side_effect=make_fake_ollama(calls)):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("中文快讯发了 1 条", len(bot.sent) == 1)
    ok &= check("中文标题原样发",
                bot.sent and "OpenAI 正式发布 GPT-5" in bot.sent[0]["text"])
    ok &= check("没调翻译", calls.get("translate", 0) == 0)
    return ok


async def test_ollama_down_fallback():
    """Ollama 全挂：打分失败按关键词直接发，翻译失败发原文，首次失败后不再调翻译。"""
    await fresh_db()
    await seed(EN_TITLE, "https://example.com/en1")
    await seed(EN_TITLE_2, "https://example.com/en2")
    bot = FakeBot()
    calls = {}

    async def dead(**kwargs):
        prompt = kwargs.get("system_prompt", "")
        if "打分" in prompt:
            calls["score"] = calls.get("score", 0) + 1
        if "翻译" in prompt:
            calls["translate"] = calls.get("translate", 0) + 1
        return None

    with patch.object(ai_news, "query_ollama_chat", side_effect=dead):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("Ollama 挂了快讯照发 2 条", len(bot.sent) == 2)
    ok &= check("发的是英文原文",
                any("OpenAI launches GPT-5" in m["text"] for m in bot.sent)
                and any("Google launches Gemini 4" in m["text"] for m in bot.sent))
    ok &= check("翻译只试了一次就熔断", calls.get("translate") == 1)
    return ok


async def test_translate_title_direct():
    ok = True
    calls = {}
    with patch.object(ai_news, "query_ollama_chat",
                      side_effect=make_fake_ollama(calls)):
        out = await ai_news._translate_title("http://x:11434", "qwen2.5:7b", EN_TITLE)
    ok &= check("直接翻译返回译文", out == ZH_TRANSLATION)
    with patch.object(ai_news, "query_ollama_chat",
                      side_effect=make_fake_ollama(calls, translate_result="")):
        out = await ai_news._translate_title("http://x:11434", "qwen2.5:7b", EN_TITLE)
    ok &= check("空译文返回 None", out is None)
    return ok


async def main():
    results = []
    results.append(await test_is_english_title())
    results.append(await test_english_translated())
    results.append(await test_chinese_not_translated())
    results.append(await test_ollama_down_fallback())
    results.append(await test_translate_title_direct())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
