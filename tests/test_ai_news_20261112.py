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
from unittest.mock import patch
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
    # 先关连接池再删文件：否则池里还连着已删的旧文件（unlinked inode），
    # 新连接走新文件，读写会分裂到两个库（test_ai_scoring 在全量跑时就因此丢过发送）。
    await close_db()
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
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("突发只发了 1 条（GPT-5）", len(bot.sent) == 1)
    ok &= check("突发内容含标题", bot.sent and "GPT-5" in bot.sent[0]["text"])
    ok &= check("突发内容含原文链接", bot.sent and "https://example.com/break1" in bot.sent[0]["text"])
    ok &= check("突发不带链接预览", bot.sent and bot.sent[0].get("disable_web_page_preview") is True)
    # 再跑一次，不应重复发
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok &= check("第二次不重复发突发", len(bot.sent) == 1)
    # 日限额：已发 1 条，再插 55 条突发，最多再发 49 条（每天上限 50）。
    # 每轮只取 20 条新入库的，所以要跑多轮才发得完。
    async with get_db() as db:
        for i in range(55):
            await db.execute(
                "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Test", f"Claude 新模型发布 {i}", f"https://example.com/break{i+2}", now, now),
            )
    with patch.object(ai_news, "_score_importance", return_value=None):
        for _ in range(4):
            await ai_news.check_breaking_news(bot)
    ok &= check("日限额：全天最多 50 条突发", len(bot.sent) == 50)
    # 动态不再直通（2026-11-13 pao：量太大，改走关键词 + AI 打分）：
    # 带关键词的动态（GPT-5 发布）照样发突发；无关键词的闲聊只进早报，不发。
    # 打分 mock 成 None（Ollama 不通 → 按关键词直接发），聚焦关键词门。
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Muse动态", "GPT-5 正式发布", "https://example.com/feed1", now, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Muse动态", "今天群里聊点别的", "https://example.com/feed2", now, now),
        )
    before_feed = len(bot.sent)
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok &= check(
        "动态走关键词门：带关键词的发",
        len(bot.sent) > before_feed
        and any("GPT-5 正式发布" in m["text"] for m in bot.sent),
    )
    ok &= check("动态不计入每日上限：RSS 预算用完照样发", len(bot.sent) == before_feed + 1)
    ok &= check(
        "动态无关键词的不发突发",
        not any("今天群里聊点别的" in m["text"] for m in bot.sent),
    )
    # 被日限额挡住没发完的 RSS 突发留到下一轮，不悄悄丢掉
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS c FROM ai_news_items WHERE source = 'Test' AND sent_breaking = 0"
        )
    ok &= check("没发完的突发留在库里", int((row or {}).get("c") or 0) == 6)
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



async def test_digest_fires_after_time():
    """早报到点后第一次循环就发：循环每 5 分钟一轮，不一定卡在正点那一分钟。"""
    await fresh_db()
    await ai_news.ensure_default_settings()
    bot = FakeBot()
    yesterday = (datetime.now(CST).date() - timedelta(days=1)).isoformat()
    now = datetime.now(CST)
    past = (now - timedelta(minutes=5)).strftime("%H:%M")
    future = (now + timedelta(minutes=10)).strftime("%H:%M")
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("TechCrunch AI", "Yesterday big AI news", "https://example.com/y1", yesterday, yesterday),
        )
    # 时间设在未来：还没到点，不发
    async with get_db() as db:
        await db.execute(
            "UPDATE ai_news_settings SET daily_time = ?, last_digest_date = '' WHERE group_id = ?",
            (future, GROUP),
        )
    await ai_news.send_daily_digest(bot)
    ok = True
    ok &= check("没到点不发早报", len(bot.sent) == 0)
    # 时间设在 5 分钟前：已过点，第一次循环就该发（旧逻辑要求正好卡在那一分钟会漏发）
    async with get_db() as db:
        await db.execute(
            "UPDATE ai_news_settings SET daily_time = ?, last_digest_date = '' WHERE group_id = ?",
            (past, GROUP),
        )
    await ai_news.send_daily_digest(bot)
    ok &= check("过点后第一次循环就发早报", len(bot.sent) == 1)
    return ok


async def test_feed_breaking_rate_limit():
    """动态快讯每轮限流：一批 5 条进来，一轮最多发 2 条，剩下的留到下一轮，不丢。"""
    await fresh_db()
    await ai_news.ensure_default_settings()
    bot = FakeBot()
    now = datetime.now(CST).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "UPDATE ai_news_settings SET max_breaking_per_day = 30 WHERE group_id = ?",
            (GROUP,),
        )
        for i in range(5):
            await db.execute(
                "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Muse动态", f"GPT-5 正式发布 动态 {i}", f"https://example.com/feedcap{i}", now, now),
            )
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("动态快讯一轮最多发 2 条", len(bot.sent) == 2)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS c FROM ai_news_items WHERE source = 'Muse动态' AND sent_breaking = 0"
        )
    ok &= check("没发完的动态留在库里", int((row or {}).get("c") or 0) == 3)
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok &= check("下一轮再发 2 条", len(bot.sent) == 4)
    with patch.object(ai_news, "_score_importance", return_value=None):
        await ai_news.check_breaking_news(bot)
    ok &= check("第三轮发完最后 1 条", len(bot.sent) == 5)
    return ok


async def test_ai_scoring():
    """AI 打分（C）：高分发、低分不发、打分失败（None）按关键词直接发。"""
    await fresh_db()
    await ai_news.ensure_default_settings()
    bot = FakeBot()
    now = datetime.now(CST).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test", "OpenAI 正式发布 GPT-5", "https://example.com/score5", now, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test", "Claude 新模型发布", "https://example.com/score2", now, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test", "Gemini 新模型发布", "https://example.com/scoreNone", now, now),
        )

    async def fake_score(base_url, model_name, title, summary=""):
        if "GPT-5" in title:
            return 5
        if "Claude" in title:
            return 2
        return None

    with patch.object(ai_news, "_score_importance", side_effect=fake_score):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("打分 5 分的发", any("GPT-5" in m["text"] for m in bot.sent))
    ok &= check("打分 2 分的不发", not any("Claude" in m["text"] for m in bot.sent))
    ok &= check("打分失败（None）按关键词直接发", any("Gemini" in m["text"] for m in bot.sent))
    ok &= check("一共发了 2 条", len(bot.sent) == 2)
    # 低分条目已标记处理，不会下一轮重打分再发
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT sent_breaking FROM ai_news_items WHERE link = ?",
            ("https://example.com/score2",),
        )
    ok &= check("低分条目已标记处理", int((row or {}).get("sent_breaking") or 0) == 1)
    return ok


async def test_bwenews_bypass():
    """方程式新闻直通：无关键词也发，不占每日上限；打分不影响它。"""
    await fresh_db()
    await ai_news.ensure_default_settings()
    bot = FakeBot()
    now = datetime.now(CST).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_items (source, title, link, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("方程式新闻", "某交易所上新某某币", "https://example.com/bwe1", now, now),
        )

    async def fake_score_low(base_url, model_name, title, summary=""):
        return 1  # 就算打 1 分，方程式也不受影响

    with patch.object(ai_news, "_score_importance", side_effect=fake_score_low):
        await ai_news.check_breaking_news(bot)
    ok = True
    ok &= check("方程式无关键词也发", len(bot.sent) == 1)
    ok &= check("方程式内容含标题", bot.sent and "某交易所上新某某币" in bot.sent[0]["text"])
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
    results.append(await test_digest_fires_after_time())
    results.append(await test_feed_breaking_rate_limit())
    results.append(await test_ai_scoring())
    results.append(await test_bwenews_bypass())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
