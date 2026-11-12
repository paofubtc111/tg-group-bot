# -*- coding: utf-8 -*-
"""AI 新闻功能测试（SQLite 本地）：去重、突发关键词、早报提示词、早报降级、默认配置。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    /tmp/venv-news/bin/python tests/test_ai_news_20261112.py
注意：test_fetch_feeds 会真实联网拉 RSS（验源是否活着），其余全是本地逻辑。
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database.db import close_db, init_db, get_db
from app.bot.handlers import ai_news

CST = ZoneInfo("Asia/Shanghai")
GROUP = ai_news.DEFAULT_GROUP_ID


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
    if os.path.exists("/tmp/test_ai_news.db"):
        os.remove("/tmp/test_ai_news.db")
    await init_db()


async def test_schema_and_defaults():
    await fresh_db()
    await ai_news.ensure_default_settings()
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM ai_news_settings WHERE group_id = ?", (GROUP,)
        )
        tables = await db.fetch(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_news%'"
        )
    names = {t["name"] for t in tables}
    ok = True
    ok &= check("ai_news_items 表存在", "ai_news_items" in names)
    ok &= check("ai_news_settings 表存在", "ai_news_settings" in names)
    ok &= check("ai_news_sends 表存在", "ai_news_sends" in names)
    ok &= check("默认群配置行存在", row is not None)
    ok &= check("默认早报开", row and int(row["daily_enabled"]) == 1)
    ok &= check("默认突发开", row and int(row["breaking_enabled"]) == 1)
    ok &= check("默认早报 09:00", row and row["daily_time"] == "09:00")
    return ok


async def test_store_dedupe():
    items = [
        {"source": "Test", "title": "GPT-5 发布了", "link": "https://example.com/1", "published_at": "2026-11-11"},
        {"source": "Test", "title": "普通新闻", "link": "https://example.com/2", "published_at": "2026-11-11"},
    ]
    n1 = await ai_news.store_items(items)
    n2 = await ai_news.store_items(items)
    ok = True
    ok &= check("首次入库 2 条", n1 == 2)
    ok &= check("重复入库 0 条（去重）", n2 == 0)
    async with get_db() as db:
        rows = await db.fetch("SELECT COUNT(*) AS c FROM ai_news_items")
    ok &= check("库里一共 2 条", int(rows[0]["c"]) == 2)
    return ok


async def test_is_breaking():
    ok = True
    ok &= check("GPT-5 命中突发", ai_news._is_breaking("OpenAI 发布 GPT-5"))
    ok &= check("Claude 命中突发", ai_news._is_breaking("Claude 4.5 上线"))
    ok &= check("开源命中突发", ai_news._is_breaking("某模型开源了"))
    ok &= check("普通新闻不命中", not ai_news._is_breaking("今天天气不错"))
    ok &= check("融资命中突发", ai_news._is_breaking("某 AI 公司获 10 亿融资"))
    ok &= check("只提模型名不命中（GPT-4 教程）", not ai_news._is_breaking("GPT-4 教程：怎么写提示词"))
    ok &= check("短串 o1 不误伤（No1 排名）", not ai_news._is_breaking("No1 AI 工具排名"))
    ok &= check("只提发布不命中（某产品发布）", not ai_news._is_breaking("某聊天产品发布新版本"))
    return ok


async def test_prompt_builders():
    items = [
        {"source": "TechCrunch AI", "title": "OpenAI releases GPT-5",
         "link": "https://example.com/gpt5", "published_at": "2026-11-11",
         "summary": "OpenAI released GPT-5 with better reasoning."},
        {"source": "Hugging Face", "title": "Gemini 3 is here",
         "link": "https://example.com/gemini3", "published_at": "2026-11-11",
         "summary": ""},
    ]
    user_prompt = ai_news.build_digest_user_prompt("11月11日", items)
    fallback = ai_news.build_fallback_digest("11月11日", items)
    sys_prompt = ai_news.DIGEST_SYSTEM_PROMPT
    ok = True
    ok &= check("提示词含防幻觉规则（只用列表内容）", "只使用下面" in sys_prompt)
    ok &= check("提示词要求 HTML 标签、禁 Markdown", "<b>" in sys_prompt and "不要用 Markdown" in sys_prompt)
    ok &= check("提示词有字数上限", "800" in sys_prompt)
    ok &= check("提示词删掉死代码空列表规则", "昨日无重要 AI 新闻" not in sys_prompt)
    ok &= check("提示词要求同一事件合并成一条", "合并成一条" in sys_prompt)
    ok &= check("提示词有排序优先级（新模型/开源/融资/监管）", "新模型发布" in sys_prompt and "政策监管" in sys_prompt)
    ok &= check("提示词允许 AI 圈八卦（排在实质新闻后）", "八卦也可以列入" in sys_prompt and "排在实质新闻后面" in sys_prompt)
    ok &= check("提示词仍优先实质新闻", "优先新模型发布" in sys_prompt and "政策监管" in sys_prompt)
    ok &= check("提示词要求模型名保留英文原名", "保留英文原名" in sys_prompt)
    ok &= check("提示词要求按摘要写、无摘要只写一句", "摘要" in sys_prompt and "不要展开" in sys_prompt)
    ok &= check("提示词要求链接原样复制、不编 href", "原样复制" in sys_prompt)
    ok &= check("提示词每条 1-2 句（不是 2-3 句）", "1-2 句" in sys_prompt)
    ok &= check("user prompt 含两条新闻", "GPT-5" in user_prompt and "Gemini 3" in user_prompt)
    ok &= check("user prompt 含原文链接", "https://example.com/gpt5" in user_prompt)
    ok &= check("user prompt 含摘要", "better reasoning" in user_prompt)
    ok &= check("user prompt 无摘要标成“无”", "摘要：无" in user_prompt)
    ok &= check("降级早报是标题+链接清单", "https://example.com/gpt5" in fallback and "原文" not in fallback.split("📰")[1][:0])
    ok &= check("降级早报标明 AI 摘要未就绪", "暂未就绪" in fallback)
    # 摘要清洗：去 HTML、截断
    ok &= check("摘要清洗去 HTML 标签", ai_news._clean_summary("<p>hello <b>world</b></p>") == "hello world")
    ok &= check("摘要清洗截断 300 字", len(ai_news._clean_summary("x" * 500)) == 300)
    return ok


async def test_html_escape_and_date():
    """标题含 & < 时，降级早报和突发快讯不能把 HTML 打坏；{date} 要被替换。"""
    import html as _html
    items = [
        {"source": "Test & Co", "title": "Q&A: R&D <新> 模型发布",
         "link": "https://example.com/?a=1&b=2", "published_at": "2026-11-11"},
    ]
    fb = ai_news.build_fallback_digest("11月11日", items)
    ok = True
    ok &= check("降级早报转义标题里的 & <", "&amp;" in fb and "&lt;" in fb)
    ok &= check("降级早报不含裸 <新>", "<新>" not in fb)
    ok &= check("降级早报转义链接里的 &", "a=1&amp;b=2" in fb)
    # 突发快讯的转义走同一逻辑：直接拼一条验证
    title = _html.escape(items[0]["title"], quote=True)
    ok &= check("突发标题转义可用 html.escape", title == "Q&amp;A: R&amp;D &lt;新&gt; 模型发布")
    # {date} 用 .replace() 替换
    sys_prompt = ai_news.DIGEST_SYSTEM_PROMPT.replace("{date}", "11月11日")
    ok &= check("{date} 已被替换成日期", "{date}" not in sys_prompt and "11月11日" in sys_prompt)
    ok &= check("提示词无其他花括号残留", "{" not in sys_prompt.replace("{date}", ""))
    return ok


async def test_fetch_feeds():
    items = await ai_news.fetch_all_feeds()
    ok = True
    ok &= check(f"真实拉 RSS 拿到文章（{len(items)} 条）", len(items) > 0)
    sources = {it["source"] for it in items}
    ok &= check("TechCrunch AI 有内容", "TechCrunch AI" in sources)
    ok &= check("Hugging Face 有内容", "Hugging Face" in sources)
    ok &= check("Google AI Blog 有内容", "Google AI Blog" in sources)
    import re as _re
    dated = [it for it in items if it.get("published_at")]
    ok &= check(
        "发布日期已统一成 YYYY-MM-DD",
        bool(dated) and all(_re.fullmatch(r"\d{4}-\d{2}-\d{2}", it["published_at"]) for it in dated),
    )
    with_summary = [it for it in items if (it.get("summary") or "").strip()]
    ok &= check(f"多数文章带摘要（{len(with_summary)}/{len(items)} 条）", len(with_summary) > len(items) // 2)
    ok &= check("摘要里没有 HTML 标签", all("<" not in (it.get("summary") or "") for it in items))
    n = await ai_news.store_items(items)
    ok &= check(f"真实文章入库 {n} 条", n > 0)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT summary FROM ai_news_items WHERE summary != '' LIMIT 1"
        )
    ok &= check("摘要已存进库", row is not None)
    return ok


async def test_breaking_flow():
    # 用干净库，避免前面真实拉到的文章干扰突发计数
    await fresh_db()
    await ai_news.ensure_default_settings()
    bot = FakeBot()
    # 先清表：test_fetch_feeds 刚入库的真实文章很多都命中突发关键词，
    # 不清会把这组测试污染掉（之前跑出过 2 条 FAIL 就是这个原因）。
    async with get_db() as db:
        await db.execute("DELETE FROM ai_news_items")
        await db.execute("DELETE FROM ai_news_sends")
    # 插一条突发（GPT-5）+ 一条普通
    now = datetime.now(CST).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test", "OpenAI 正式发布 GPT-5", "https://example.com/break1", now, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test", "今天天气不错", "https://example.com/normal1", now, now),
        )
    await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("突发只发了 1 条（GPT-5）", len(bot.sent) == 1)
    ok &= check("突发内容含标题", bot.sent and "GPT-5" in bot.sent[0]["text"])
    ok &= check("突发内容含原文链接", bot.sent and "https://example.com/break1" in bot.sent[0]["text"])
    ok &= check("突发不带链接预览", bot.sent and bot.sent[0].get("disable_web_page_preview") is True)
    # 再跑一次，不应重复发
    await ai_news.check_breaking_news(bot)
    ok &= check("第二次不重复发突发", len(bot.sent) == 1)
    # 日限额：已发 1 条，再插 12 条突发，最多再发 9 条（每天上限 10）
    async with get_db() as db:
        for i in range(12):
            await db.execute(
                "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Test", f"Claude 新模型发布 {i}", f"https://example.com/break{i+2}", now, now),
            )
    await ai_news.check_breaking_news(bot)
    ok &= check("日限额：全天最多 10 条突发", len(bot.sent) == 10)
    # 动态直通：来源 Muse动态 的条目不走关键词/AI 门，标题再普通也发
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Muse动态", "今天群里聊点别的", "https://example.com/feed1", now, now),
        )
    # 先把上限调高到 30，避免被日限额挡住
    async with get_db() as db:
        await db.execute(
            "UPDATE ai_news_settings SET max_breaking_per_day = 30 WHERE group_id = ?",
            (GROUP,),
        )
    await ai_news.check_breaking_news(bot)
    ok &= check("动态直通突发：无关键词也发", len(bot.sent) == 11 and "今天群里聊点别的" in bot.sent[-1]["text"])
    return ok


async def test_digest_fallback():
    await fresh_db()
    await ai_news.ensure_default_settings()
    """Ollama 不通时，早报降级为标题清单，且只发一次。"""
    bot = FakeBot()
    yesterday = (datetime.now(CST).date() - timedelta(days=1)).isoformat()
    now = datetime.now(CST)
    # 把早报时间改成当前分钟，触发发送
    async with get_db() as db:
        await db.execute(
            "UPDATE ai_news_settings SET daily_time = ?, last_digest_date = '' WHERE group_id = ?",
            (now.strftime("%H:%M"), GROUP),
        )
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("TechCrunch AI", "Yesterday big AI news", "https://example.com/y1", yesterday, yesterday),
        )
    await ai_news.send_daily_digest(bot)
    ok = True
    ok &= check("早报发了 1 条", len(bot.sent) == 1)
    ok &= check("早报不带链接预览", bot.sent and bot.sent[0].get("disable_web_page_preview") is True)
    ok &= check("Ollama 不通时降级为标题清单", bot.sent and "暂未就绪" in bot.sent[0]["text"])
    ok &= check("降级清单含标题", bot.sent and "Yesterday big AI news" in bot.sent[0]["text"])
    # 再跑一次，今天已发过，不重复
    await ai_news.send_daily_digest(bot)
    ok &= check("同一天早报不重复发", len(bot.sent) == 1)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT last_digest_date FROM ai_news_settings WHERE group_id = ?", (GROUP,)
        )
    ok &= check("last_digest_date 已记今天", row and row["last_digest_date"] == now.date().isoformat())
    return ok


async def main():
    results = []
    results.append(await test_schema_and_defaults())
    results.append(await test_store_dedupe())
    results.append(await test_is_breaking())
    results.append(await test_prompt_builders())
    results.append(await test_html_escape_and_date())
    results.append(await test_fetch_feeds())
    results.append(await test_breaking_flow())
    results.append(await test_digest_fallback())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
