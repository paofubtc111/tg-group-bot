# -*- coding: utf-8 -*-
"""早报中文保险测试（v2.4）：中文占比判定 + 英文太多自动重生成一次。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    ~/workspace/.venv-bot-test/bin/python tests/test_digest_language_guard_20261125.py
全是本地逻辑，不联网、不调真 Ollama（query_ollama_chat 用假函数替掉）。
"""

import asyncio
import os
import sys
from unittest.mock import patch

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news_guard.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.bot.handlers import ai_news

GROUP = -1004496842671

CHINESE_DIGEST = (
    "📰 昨日 AI 大事件（11月24日）\n\n"
    "<b>GPT-5 发布新版本</b>\n"
    "OpenAI 发布了 GPT-5 的新版本，推理能力明显提升，开发者可以直接调用。\n"
    "<a href=\"https://example.com/a\">原文</a>\n\n"
    "<b>Qwen 开源新模型</b>\n"
    "新模型在多个榜单上超过了同尺寸的开源模型，适合本地部署。\n"
    "<a href=\"https://example.com/b\">原文</a>"
)

ENGLISH_DIGEST = (
    "📰 Yesterday's AI Highlights (Nov 24)\n\n"
    "<b>GPT-5 Launches New Version</b>\n"
    "OpenAI released a new version of GPT-5 with much better reasoning, "
    "and developers can call it directly.\n"
    "<a href=\"https://example.com/a\">Source</a>"
)

MOSTLY_ENGLISH_DIGEST = (
    "<b>GPT-5 Launches</b>\nOpenAI released a new version today. "
    "中文只有几个字。"
)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def test_chinese_ratio():
    ok = True
    ok &= check("中文早报占比 > 0.7", ai_news._chinese_ratio(CHINESE_DIGEST) > 0.7)
    ok &= check("纯英文早报占比 < 0.1", ai_news._chinese_ratio(ENGLISH_DIGEST) < 0.1)
    ok &= check("中英混合占比介于两者之间",
                0.1 < ai_news._chinese_ratio(MOSTLY_ENGLISH_DIGEST) < 0.7)
    ok &= check("空文本占比 0", ai_news._chinese_ratio("") == 0.0)
    ok &= check("只有链接和标签不算数",
                ai_news._chinese_ratio('<a href="https://example.com/abc">原文</a>') == 1.0)
    return ok


def _make_fake(results):
    """按顺序返回 results 里的摘要，记录每次调用的 system_prompt。"""
    calls = []

    async def fake(base_url, model_name, system_prompt, history, user_prompt):
        calls.append(system_prompt)
        return results[len(calls) - 1]

    fake.calls = calls
    return fake


async def test_guard_retries_on_english():
    fake = _make_fake([ENGLISH_DIGEST, CHINESE_DIGEST])
    with patch.object(ai_news, "query_ollama_chat", fake):
        out = await ai_news._generate_digest_with_language_guard(
            "http://x", "qwen2.5:7b", "sys", "user", GROUP
        )
    ok = True
    ok &= check("英文首版触发重生成（调了 2 次）", len(fake.calls) == 2)
    ok &= check("重生成后返回中文版", out == CHINESE_DIGEST)
    ok &= check("第二次带强制中文提醒",
                ai_news.DIGEST_RETRY_SYSTEM_SUFFIX.strip() in fake.calls[1])
    return ok


async def test_guard_no_retry_on_chinese():
    fake = _make_fake([CHINESE_DIGEST])
    with patch.object(ai_news, "query_ollama_chat", fake):
        out = await ai_news._generate_digest_with_language_guard(
            "http://x", "qwen2.5:7b", "sys", "user", GROUP
        )
    ok = True
    ok &= check("中文首版不重生成（只调 1 次）", len(fake.calls) == 1)
    ok &= check("中文首版原样返回", out == CHINESE_DIGEST)
    return ok


async def test_guard_both_english_keeps_better():
    fake = _make_fake([MOSTLY_ENGLISH_DIGEST, ENGLISH_DIGEST])
    with patch.object(ai_news, "query_ollama_chat", fake):
        out = await ai_news._generate_digest_with_language_guard(
            "http://x", "qwen2.5:7b", "sys", "user", GROUP
        )
    ok = True
    ok &= check("两次都英文也只调 2 次", len(fake.calls) == 2)
    ok &= check("两次都英文发占比更高的一版", out == MOSTLY_ENGLISH_DIGEST)
    return ok


async def test_guard_none_no_retry():
    fake = _make_fake([None])
    with patch.object(ai_news, "query_ollama_chat", fake):
        out = await ai_news._generate_digest_with_language_guard(
            "http://x", "qwen2.5:7b", "sys", "user", GROUP
        )
    ok = True
    ok &= check("首版为空不重生成（只调 1 次，走降级）", len(fake.calls) == 1)
    ok &= check("首版为空返回 None", out is None)
    return ok


async def main():
    ok = True
    ok &= test_chinese_ratio()
    ok &= await test_guard_retries_on_english()
    ok &= await test_guard_no_retry_on_chinese()
    ok &= await test_guard_both_english_keeps_better()
    ok &= await test_guard_none_no_retry()
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
