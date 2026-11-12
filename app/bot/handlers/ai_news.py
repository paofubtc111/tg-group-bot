# -*- coding: utf-8 -*-
"""
AI 新闻：每日早报 + 突发快讯。

来源：AI 媒体 RSS，每 30 分钟拉一次，链接去重后存库。
- 每日早报：每天指定时间（默认 09:00），把昨天的新新闻交给 qwen2.5
翻译+摘要成中文早报发到群里；Ollama 不通时降级为标题+链接清单。
- 突发快讯：新文章命中突发关键词就立刻发（标题+来源+链接，不走 LLM，
这样 Ollama 断了也能发）；每群每天最多 3 条，防刷屏。

默认只发到“拥抱A i”（-1004496842671），其他群需在 ai_news_settings 里打开。
面板配置入口后续再加；现在改配置直接改库或找 Ryan。
"""

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import httpx
from aiogram import Bot

from app.config import settings
from app.database.db import get_db
from app.bot.handlers.llm_chat import query_ollama_chat

logger = logging.getLogger("tg-group-bot.ai_news")

CST = ZoneInfo("Asia/Shanghai")

# 默认只发到这个群（pao 定的）
DEFAULT_GROUP_ID = -1004496842671
DEFAULT_DAILY_TIME = "09:00"

# RSS 源：(来源名, feed 地址)
RSS_FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    ("36氪", "https://www.36kr.com/feed"),
]

# 突发关键词：分两档，避免刷屏。
# - 高信号词：单独出现就发（收购/融资/起诉/禁令/泄露/宕机/开源……）。
# - 模型名 + 事件词：两个都出现才发（只提模型名不发，如“GPT-4 教程”）。
# 注意：不要把 "o1"/"o3"/"o4" 这类短串当关键词——子串匹配会误伤
# （如 "No1" 里就有 "o1"）；模型发布走“模型名 + 发布/上线”那档。
BREAKING_HIGH_SIGNAL = [
    "收购", "并购", "acquisition", "融资", "funding", "ipo", "上市",
    "起诉", "lawsuit", "禁令", "ban", "监管", "regulation",
    "泄露", "leak", "宕机", "outage", "安全漏洞",
    "开源", "open source",
]
BREAKING_MODELS = [
    "gpt-5", "gpt-4", "gpt-4o", "gpt-4.5",
    "claude", "gemini", "grok", "llama", "qwen", "deepseek",
    "mistral", "kimi", "glm", "doubao", "豆包",
]
BREAKING_EVENTS = [
    "发布", "推出", "上线", "release", "launch",
    "新模型", "新一代", "推理模型", "多模态",
]

MAX_BREAKING_PER_DAY = 3
FETCH_INTERVAL_SECONDS = 1800
LOOP_SLEEP_SECONDS = 300

# ---------------- qwen2.5 早报提示词 ----------------
# pao 要求：必须给 qwen2.5 写好提示词，否则它写不好。
# 2026-11-12 修了 3 处（之前版本的问题）：
# 1) 只给标题、不给摘要：qwen 只能凭标题编 2-3 句话，必然幻觉。
#    现在每条都带 RSS 摘要（见 build_digest_user_prompt），提示词也写明
#    “按摘要写；没有摘要的只按标题写一句话，不展开”。
# 2) 链接占位符写成 <a href="原文链接">，模型会照抄“原文链接”四个字当网址。
#    改成“把列表里的链接原样复制进 href”，并加一条硬性规则。
# 3) “新闻列表为空只回一句话”是死代码：没新闻时代码根本不调 LLM，
#    直接不发。删掉这条，换成日期规则（早报写的是昨天，别写成今天）。
# 另外把每条从 2-3 句收成 1-2 句：7B 小模型在 60 秒超时内写完更稳。

DIGEST_SYSTEM_PROMPT = """你是一名 AI 科技新闻编辑，负责把 AI 新闻整理成中文群聊早报，直接发到 Telegram 群里。

【输出格式】
- 开头一行：📰 昨日 AI 大事件（{date}）
- 然后是最重要的 3-5 条新闻，每条格式固定为：
  <b>标题（中文，不超过 20 字，模型/产品名保留英文原名，如 GPT-5、Gemini、Qwen）</b>
  1-2 句话讲清楚发生了什么、为什么重要。
  <a href="原文链接">原文</a>
- 条与条之间空一行。
- 全文不超过 800 字。

【硬性规则】
1. 只使用下面“新闻列表”里的内容，不编造、不补充列表之外的消息；不确定的内容不要写。
2. 同一事件多家报道合并成一条，不要重复列。
3. 按重要性排序，只保留最重要的 3-5 条：优先新模型发布、产品上线、开源、技术突破、重大融资/并购、政策监管；名人私生活、八卦类新闻，除非导致 CEO 下台、公司重大变动，否则不列入。
4. 每条新闻的 1-2 句话必须根据它自己的“摘要”来写；如果某条写着“摘要：无”（如 Hugging Face 只有标题），只按标题写一句话，不要展开、不要编细节。
5. 英文标题和摘要翻译成准确、自然的中文，不要生硬直译；已经是中文的标题直接用，不要改写；模型、产品、公司名保留英文原名。
6. 链接必须原样复制列表里给的原文链接，一个字都不能改、不能编；不要把“原文链接”四个字放进 href 里。
7. 只用上面规定的 HTML 标签（<b>、<a href="">），不要用 Markdown（**、[]()），不要用其他标签。
8. 输出直接就是早报正文，不要加“好的”“以下是”等客套话，不要解释你的工作过程。
9. 日期就是标题里那个昨天日期，不要写成今天或明天。"""


def _clean_summary(raw: str, limit: int = 300) -> str:
    """RSS 摘要去 HTML 标签、去实体、压空白、截断。36氪的摘要是整篇 HTML，
    不洗就直接把几千字塞给 qwen。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _entry_date(entry) -> str:
    """发布时间统一成 YYYY-MM-DD（feed 的 published 字符串格式各不相同，
    有的 "Thu, 12 Nov 2026 ..."、有的 ISO，直接存原串会导致早报按日期
    查不到）。优先用 feedparser 解析好的 published_parsed。"""
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed:
        try:
            return time.strftime("%Y-%m-%d", parsed)
        except Exception:
            pass
    raw = (getattr(entry, "published", "") or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)
    return raw


def build_digest_user_prompt(date_str: str, items: list) -> str:
    """把新闻列表拼成 user prompt。每条带摘要——没有摘要就写“无”，
    让 qwen 知道这条只能按标题写一句话，别编。"""
    lines = [
        f"日期：{date_str}（汇总的是昨天的新闻）",
        "新闻列表（标题 | 来源 | 链接 | 发布日期 | 摘要）：",
    ]
    for i, it in enumerate(items, 1):
        summary = (it.get("summary") or "").strip() or "无"
        lines.append(
            f"{i}. {it.get('title', '')} | {it.get('source', '')} | "
            f"{it.get('link', '')} | {it.get('published_at', '')} | 摘要：{summary}"
        )
    lines.append("请按上面的规则，输出一份「昨日 AI 大事件」早报。")
    return "\n".join(lines)


def build_fallback_digest(date_str: str, items: list) -> str:
    """Ollama 不通时的降级早报：标题+来源+链接清单，不走 LLM。"""
    lines = [f"📰 <b>昨日 AI 大事件（{date_str}）</b>", ""]
    for it in items[:8]:
        title = html.escape(it.get("title", ""), quote=True)
        link = html.escape(it.get("link", ""), quote=True)
        source = html.escape(it.get("source", ""), quote=True)
        lines.append(f"• <a href=\"{link}\">{title}</a>（{source}）")
    lines.append("")
    lines.append("（AI 摘要服务暂未就绪，先发标题清单。）")
    return "\n".join(lines)


async def fetch_all_feeds() -> list:
    """拉全部 RSS 源，返回文章列表（不去重，去重在入库时做）。"""
    out = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for source, url in RSS_FEEDS:
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                for entry in feed.entries[:20]:
                    title = (getattr(entry, "title", "") or "").strip()
                    link = (getattr(entry, "link", "") or "").strip()
                    if not title or not link:
                        continue
                    # 摘要是给 qwen 写早报的素材，必须带上；Hugging Face 等源没有
                    # 摘要字段时留空，提示词里会让模型只按标题写一句话。
                    raw_summary = getattr(entry, "summary", "") or getattr(
                        entry, "description", ""
                    ) or ""
                    if not raw_summary:
                        content = getattr(entry, "content", None) or []
                        if content:
                            raw_summary = content[0].get("value", "") or ""
                    out.append(
                        {
                            "source": source,
                            "title": title,
                            "link": link,
                            "published_at": _entry_date(entry),
                            "summary": _clean_summary(raw_summary),
                        }
                    )
            except Exception as exc:
                logger.warning("RSS 拉取失败 (%s %s): %s", source, url, exc)
    return out


async def store_items(items: list) -> int:
    """入库去重（link 唯一）。返回新入库条数。"""
    if not items:
        return 0
    now = datetime.now(CST).isoformat(timespec="seconds")
    new_count = 0
    async with get_db() as db:
        for it in items:
            try:
                rowid = await db.execute(
                    "INSERT OR IGNORE INTO ai_news_items "
                    "(source, title, link, published_at, fetched_at, summary) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (it["source"], it["title"], it["link"], it.get("published_at", ""), now,
                     it.get("summary", "")),
                )
                if rowid:
                    new_count += 1
            except Exception as exc:
                logger.warning("AI 新闻入库失败 (%s): %s", it.get("link", ""), exc)
    return new_count


async def ensure_default_settings() -> None:
    """确保默认群（拥抱A i）有一行配置：早报 09:00 开、突发开。其他群没行 = 不发。"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_settings "
            "(group_id, daily_enabled, daily_time, breaking_enabled) "
            "VALUES (?, 1, ?, 1)",
            (DEFAULT_GROUP_ID, DEFAULT_DAILY_TIME),
        )


async def get_enabled_groups(kind: str) -> list:
    """取开着某类新闻的群配置。kind: 'daily' 或 'breaking'。"""
    col = "daily_enabled" if kind == "daily" else "breaking_enabled"
    async with get_db() as db:
        rows = await db.fetch(
            f"SELECT * FROM ai_news_settings WHERE {col} = 1"
        )
    return rows


async def get_group_llm(group_id: int) -> tuple:
    """取某群的 Ollama 地址和模型（群配置优先，没有则用全局默认）。"""
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT base_url, model_name FROM group_llm_settings WHERE group_id = ?",
            (group_id,),
        )
    if row and (row.get("base_url") or "").strip():
        return row["base_url"].strip(), (row.get("model_name") or "qwen2.5:7b").strip()
    return settings.OLLAMA_BASE_URL, "qwen2.5:7b"


def _parse_keywords(text: str) -> list:
    """逗号分隔的关键词串 → 去重后的词列表。支持英文逗号、中文逗号、顿号。"""
    if not text:
        return []
    parts = text.replace("，", ",").replace("、", ",").split(",")
    seen = set()
    out = []
    for part in parts:
        kw = part.strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            out.append(kw)
    return out


def _breaking_keywords(cfg: dict) -> tuple:
    """取某群的突发关键词三档；没配（空串）就用模块默认值。"""
    cfg = cfg or {}
    high = _parse_keywords(cfg.get("breaking_high_signal") or "")
    models = _parse_keywords(cfg.get("breaking_models") or "")
    events = _parse_keywords(cfg.get("breaking_events") or "")
    return (
        high or BREAKING_HIGH_SIGNAL,
        models or BREAKING_MODELS,
        events or BREAKING_EVENTS,
    )


def _is_breaking(title: str, high=None, models=None, events=None) -> bool:
    lowered = title.lower()
    if any(kw.lower() in lowered for kw in (high or BREAKING_HIGH_SIGNAL)):
        return True
    has_model = any(kw.lower() in lowered for kw in (models or BREAKING_MODELS))
    has_event = any(kw.lower() in lowered for kw in (events or BREAKING_EVENTS))
    return has_model and has_event


async def check_breaking_news(bot: Bot) -> None:
    """把新入库且命中突发关键词的文章发出去（标题+来源+链接，不走 LLM）。"""
    groups = await get_enabled_groups("breaking")
    if not groups:
        return
    async with get_db() as db:
        fresh = await db.fetch(
            "SELECT * FROM ai_news_items WHERE sent_breaking = 0 ORDER BY id DESC LIMIT 20"
        )
    if not fresh:
        return
    # 每群用自己的关键词算突发（没配就用默认词）
    group_breaking = {}
    any_breaking = False
    for cfg in groups:
        high, models, events = _breaking_keywords(cfg)
        items = [
            it for it in fresh
            if _is_breaking(it.get("title", ""), high, models, events)
        ]
        group_breaking[cfg["group_id"]] = items
        if items:
            any_breaking = True
    if not any_breaking:
        async with get_db() as db:
            for it in fresh:
                await db.execute(
                    "UPDATE ai_news_items SET sent_breaking = 1 WHERE id = ?", (it["id"],)
                )
        return
    today = datetime.now(CST).date().isoformat()
    now_iso = datetime.now(CST).isoformat(timespec="seconds")
    for cfg in groups:
        group_id = cfg["group_id"]
        breaking = group_breaking.get(group_id) or []
        if not breaking:
            continue
        async with get_db() as db:
            sent_today = await db.fetchrow(
                "SELECT COUNT(*) AS c FROM ai_news_sends "
                "WHERE group_id = ? AND kind = 'breaking' AND substr(sent_at, 1, 10) = ?",
                (group_id, today),
            )
        already = int((sent_today or {}).get("c") or 0)
        budget = MAX_BREAKING_PER_DAY - already
        if budget <= 0:
            continue
        for it in breaking[:budget]:
            title = html.escape(it.get("title", ""), quote=True)
            link = html.escape(it.get("link", ""), quote=True)
            source = html.escape(it.get("source", ""), quote=True)
            text = (
                f"🚨 <b>AI 快讯</b>\n\n"
                f"<b>{title}</b>\n"
                f"来源：{source}\n"
                f"<a href=\"{link}\">原文</a>"
            )
            try:
                await bot.send_message(chat_id=group_id, text=text)
                logger.info("突发快讯已发 (group=%s, link=%s)", group_id, it.get("link", ""))
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO ai_news_sends (item_id, group_id, kind, sent_at) "
                        "VALUES (?, ?, 'breaking', ?)",
                        (it["id"], group_id, now_iso),
                    )
            except Exception as exc:
                logger.warning("突发快讯发送失败 (group=%s): %s", group_id, exc)
    async with get_db() as db:
        for it in fresh:
            await db.execute(
                "UPDATE ai_news_items SET sent_breaking = 1 WHERE id = ?", (it["id"],)
            )


async def send_daily_digest(bot: Bot) -> None:
    """到点的群发昨日早报。没新闻就不发；Ollama 不通降级为标题清单。"""
    groups = await get_enabled_groups("daily")
    if not groups:
        return
    now = datetime.now(CST)
    today_str = now.date().isoformat()
    yesterday = now.date() - timedelta(days=1)
    for cfg in groups:
        group_id = cfg["group_id"]
        daily_time = (cfg.get("daily_time") or DEFAULT_DAILY_TIME).strip()
        if daily_time != now.strftime("%H:%M"):
            continue
        if (cfg.get("last_digest_date") or "") == today_str:
            continue
        async with get_db() as db:
            items = await db.fetch(
                "SELECT * FROM ai_news_items "
                "WHERE substr(fetched_at, 1, 10) = ? OR substr(published_at, 1, 10) = ? "
                "ORDER BY id DESC LIMIT 10",
                (yesterday.isoformat(), yesterday.isoformat()),
            )
        if not items:
            async with get_db() as db:
                await db.execute(
                    "UPDATE ai_news_settings SET last_digest_date = ? WHERE group_id = ?",
                    (today_str, group_id),
                )
            continue
        date_label = f"{yesterday.month}月{yesterday.day}日"
        base_url, model_name = await get_group_llm(group_id)
        system_prompt = DIGEST_SYSTEM_PROMPT.replace("{date}", date_label)
        user_prompt = build_digest_user_prompt(date_label, items)
        digest = await query_ollama_chat(
            base_url=base_url,
            model_name=model_name,
            system_prompt=system_prompt,
            history=[],
            user_prompt=user_prompt,
        )
        if not digest:
            digest = build_fallback_digest(date_label, items)
            logger.warning("早报 LLM 摘要失败 (group=%s)，已降级为标题清单", group_id)
        try:
            await bot.send_message(chat_id=group_id, text=digest)
            logger.info("每日早报已发 (group=%s, %s 条新闻)", group_id, len(items))
        except Exception as exc:
            logger.warning("每日早报发送失败 (group=%s): %s", group_id, exc)
            continue
        async with get_db() as db:
            await db.execute(
                "UPDATE ai_news_settings SET last_digest_date = ? WHERE group_id = ?",
                (today_str, group_id),
            )
            for it in items:
                await db.execute(
                    "UPDATE ai_news_items SET sent_digest = 1 WHERE id = ?", (it["id"],)
                )


async def ai_news_loop(bot: Bot, interval_seconds: int = LOOP_SLEEP_SECONDS) -> None:
    """AI 新闻后台循环：每 30 分钟拉一次 RSS + 查突发；每轮检查早报到点没到。"""
    logger.info("AI 新闻循环已启动 (RSS 每 30 分钟，循环每 %s 秒)", interval_seconds)
    await ensure_default_settings()
    last_fetch = 0.0
    while True:
        try:
            now_ts = datetime.now(CST).timestamp()
            if now_ts - last_fetch >= FETCH_INTERVAL_SECONDS:
                items = await fetch_all_feeds()
                new_count = await store_items(items)
                if new_count:
                    logger.info("RSS 拉取完成，新入库 %s 条", new_count)
                last_fetch = now_ts
                await check_breaking_news(bot)
            await send_daily_digest(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("AI 新闻循环异常: %s", exc)
        await asyncio.sleep(interval_seconds)
