# -*- coding: utf-8 -*-
"""
AI 新闻：每日早报 + 突发快讯。

来源：AI 媒体 RSS + Muse 动态 + 方程式新闻，每 30 分钟拉一次，链接去重后存库。
- 每日早报：每天指定时间（默认 09:00），把昨天的新新闻交给 qwen2.5
翻译+摘要成中文早报发到群里；Ollama 不通时降级为标题+链接清单。
方程式新闻是加密快讯，不进 AI 早报。
- 突发快讯：三层筛选（学方程式新闻：正则 + AI + 人工，这里人工换成早报兜底）。
  1) 关键词初筛：高信号词 + AI 相关，或模型名 + 事件词；
  2) qwen2.5 打分 1-5，>= 4 才发，1-3 只进早报；Ollama 不通时按关键词直接发；
  3) 方程式新闻：只发 AI 相关 + 盗窃/黑客类安全事件（只限 6 小时内的新快讯），
     其余币圈内容（上币/监管/融资/人事）不发；过 filter 的直接转发，不打分。
  每群每天最多 50 条（只算 RSS 突发；动态、方程式不占名额），
  动态每轮每群最多 2 条防刷屏。

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
# 每轮循环（5 分钟）每群最多发几条动态快讯。动态是整批入库的，
# 不限流会一分钟连发 10 条挤爆群聊；没发完的留在库里下一轮再发，不丢。
MAX_FEED_BREAKING_PER_CYCLE = 2

# 方程式新闻：只转发 6 小时内发布的新快讯。RSS 首次拉取会带回最近 20 条
# （约 10 天的量），不加新鲜度过滤会一次全发出来刷屏；旧的直接不入库。
BWENEWS_MAX_AGE_HOURS = 6
# AI 打分门槛：1-5 分，>= 4 才发突发（4 = 重要，5 = 重大）；1-3 只进早报。
IMPORTANCE_THRESHOLD = 4
# 打分单次超时（秒）：Ollama 在 pao 电脑上，断了不能让快讯循环卡 60 秒。
SCORE_TIMEOUT_SECONDS = 20

RSS_FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    # 方程式新闻 BWEnews（加密快讯，@BWEnews）：主频道中文 RSS，免费免 Key。
    # pao 2026-11-13：他发什么直接转发。已是精选源（月均 ~58 条），不走关键词/AI 门。
    ("方程式新闻", "https://api.eqn.news/public/rss?ch=main&lang=zh"),
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

# AI 相关性词：第一档高信号词必须同时命中其中之一才算突发。
# 否则 36氪这类综合源的非 AI 商业新闻（京东收购零售、华尔街监管、股票退市）
# 只要带“收购/监管/上市”就会误发。
AI_TERMS = [
    # 模型与 AI 产品/公司
    "gpt", "claude", "gemini", "grok", "llama", "qwen", "deepseek",
    "mistral", "kimi", "glm", "doubao", "豆包", "openai", "anthropic",
    "chatgpt", "llm", "aigc", "ai",
    # AI 概念
    "人工智能", "大模型", "模型", "机器学习", "深度学习", "神经网络",
    "算力", "gpu", "生成式", "多模态", "智能体", "大语言模型",
    # AI 芯片与大厂
    "英伟达", "nvidia", "芯片", "算法",
    # 模型发布与 API（覆盖动态里那种：开源权重、机器人模型、OpenRouter 价格）
    "权重", "机器人", "具身智能", "openrouter", "api", "推理", "token",
    "基座模型", "开源模型", "agent",
]

MAX_BREAKING_PER_DAY = 50
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
3. 按重要性排序，只保留最重要的 3-5 条：优先新模型发布、产品上线、开源、技术突破、重大融资/并购、政策监管；AI 圈的八卦也可以列入（比如 CEO 绯闻、董事会逼宫这类，群里喜闻乐见），但排在实质新闻后面。
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


def _entry_published_dt(entry):
    """取 RSS 条目的发布时间（aware datetime，UTC）；解析失败返回 None。"""
    from datetime import timezone as _tz
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=_tz.utc)
        except Exception:
            return None
    return None


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
                    # 方程式新闻只收 6 小时内的新快讯：RSS 首次会带回最近 20 条
                    # （约 10 天的量），全入库会一次刷屏；旧的直接丢弃。
                    if source == "方程式新闻":
                        dt = _entry_published_dt(entry)
                        if dt is None:
                            continue
                        from datetime import timezone as _tz
                        if datetime.now(_tz.utc) - dt > timedelta(hours=BWENEWS_MAX_AGE_HOURS):
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


def _extract_first_link(body_md: str) -> str:
    """从动态正文（Markdown）里取第一个原文链接。"""
    m = re.search(r"https?://[^\s)\]]+", body_md or "")
    return m.group(0).rstrip(".,") if m else ""


def feed_unit_to_item(unit: dict) -> dict | None:
    """Muse 动态单元 → ai_news_items 的一条。
    unit: {title, body_md, source, published_at, unit_id}。
    链接取正文第一个原文链接；没有链接时用单元 id 做去重键。"""
    if not isinstance(unit, dict):
        return None
    title = (unit.get("title") or "").strip()
    body_md = unit.get("body_md") or ""
    if not title:
        return None
    link = _extract_first_link(body_md)
    if not link:
        uid = (unit.get("unit_id") or "").strip()
        if not uid:
            return None
        link = "muse-feed://" + uid
    # 摘要：把 [文字](链接) 还原成文字，再按 RSS 摘要同一套清洗（去标签、截断 300 字）
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body_md)
    return {
        "source": (unit.get("source") or "Muse动态").strip() or "Muse动态",
        "title": title,
        "link": link,
        "published_at": (unit.get("published_at") or "").strip(),
        "summary": _clean_summary(plain),
    }


async def import_feed_units(units: list) -> dict:
    """导入一批动态单元（入库去重，link 唯一）。返回导入/重复/收到条数。"""
    items = []
    for u in units or []:
        it = feed_unit_to_item(u)
        if it:
            items.append(it)
    n = await store_items(items)
    return {"imported": n, "duplicates": len(items) - n, "received": len(units or [])}


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
        # 一次性迁移（2026-11-13 上限提到 50）：把还停留在旧默认 3/10 的行提到 50。
        # pao 若之后在面板里主动设回 3 或 10，请删掉这两行，否则重启会再提到 50。
        await db.execute(
            "UPDATE ai_news_settings SET max_breaking_per_day = 50 "
            "WHERE max_breaking_per_day IN (3, 10)"
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


def _max_breaking_per_day(cfg: dict) -> int:
    """取某群的每日突发上限；没配或非法就用默认 50（限制 1-50）。"""
    try:
        v = int((cfg or {}).get("max_breaking_per_day") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if 1 <= v <= 50 else MAX_BREAKING_PER_DAY


def _is_feed_item(item: dict) -> bool:
    """是否来自 Muse 动态：不计入每日上限、每轮限流 2 条。
    （2026-11-13 改：动态不再直通突发，改走关键词 + AI 打分，和 RSS 同一套门。）"""
    return (item.get("source", "") or "").startswith("Muse")


def _is_bwenews_item(item: dict) -> bool:
    """是否来自方程式新闻：pao 2026-11-13 定，他发什么直接转发，
    不走关键词/AI 门、不占每日上限（内容过滤见 _is_bwenews_wanted）。"""
    return (item.get("source", "") or "").startswith("方程式")


# 方程式新闻是加密快讯，pao 2026-11-13 定：不要币圈内容，只发 AI 相关
# 的，和盗窃/黑客这类安全事件（上币、监管、融资、人事等一律不发）。
BWENEWS_SECURITY_TERMS = [
    "盗窃", "被盗", "盗币", "盗取", "窃取",
    "黑客", "hack", "入侵", "攻击", "breach",
    "勒索", "ransomware", "查获", "诈骗",
]


def _is_bwenews_wanted(title: str) -> bool:
    """方程式新闻这条要不要转发：AI 相关，或盗窃/黑客类安全事件才发。
    其余币圈内容（上币、监管、融资、人事等）不发。"""
    if _is_ai_related(title):
        return True
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in BWENEWS_SECURITY_TERMS)


SCORE_SYSTEM_PROMPT = """你是一名 AI 科技新闻编辑。给下面这条新闻的重要性打分，只输出一个 1-5 的数字，不要输出其他内容。

打分标准：
5 = 重大事件：旗舰大模型发布、IPO/上市、巨额融资/并购、监管/禁令/起诉、重大安全事件、大厂 CEO 变动
4 = 重要：重要模型/产品发布、开源权重、技术突破、重要政策
3 = 一般：常规产品更新、中小模型发布、行业动态
2 = 次要：小功能更新、教程、观点文章
1 = 不重要：八卦、重复报道、无实质内容

只输出数字。"""


async def _score_importance(base_url: str, model_name: str, title: str, summary: str = "") -> int | None:
    """用 Ollama 给新闻重要性打分 1-5；Ollama 不可用/超时/解析失败返回 None。
    调用方把 None 当“打分失败”，按关键词直接发，保证 Ollama 断了快讯不断。"""
    user = f"标题：{title}\n摘要：{(summary or '无')[:200]}"
    try:
        content = await asyncio.wait_for(
            query_ollama_chat(
                base_url=base_url,
                model_name=model_name,
                system_prompt=SCORE_SYSTEM_PROMPT,
                history=[],
                user_prompt=user,
            ),
            timeout=SCORE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("AI 打分失败（%s）：%s", title[:30], exc)
        return None
    if not content:
        return None
    m = re.search(r"[1-5]", content.strip())
    return int(m.group(0)) if m else None


def _is_ai_related(title: str, ai_terms=None) -> bool:
    """标题是否和 AI 相关（模型名、AI 概念、AI 公司/芯片）。"""
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in (ai_terms or AI_TERMS))


def _is_breaking(title: str, high=None, models=None, events=None, ai_terms=None) -> bool:
    lowered = title.lower()
    # 第一档：高信号词 + 必须和 AI 相关（防综合源的非 AI 商业新闻误发）
    if any(kw.lower() in lowered for kw in (high or BREAKING_HIGH_SIGNAL)):
        # AI 相关 = AI 词表，或（自定义/默认的）模型名
        if _is_ai_related(title, ai_terms):
            return True
        model_list = models or BREAKING_MODELS
        return any(kw.lower() in lowered for kw in model_list)
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
    # 每群用自己的关键词算突发（没配就用默认词）。
    # 动态不再直通（2026-11-13 pao：量太大，改走关键词 + AI 打分）；
    # 方程式新闻直通（pao：他发什么直接转发）。
    group_breaking = {}
    any_breaking = False
    for cfg in groups:
        high, models, events = _breaking_keywords(cfg)
        items = [
            it for it in fresh
            if (_is_bwenews_item(it) and _is_bwenews_wanted(it.get("title", "")))
            or _is_breaking(it.get("title", ""), high, models, events)
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
    # AI 打分（C）：给过关键词的候选条目打分，低分只进早报不发快讯。
    # 方程式新闻不打分（直通）。Ollama 不可用时整轮跳过打分、按关键词直接发，
    # 保证快讯不断；熔断后本轮剩余条目不再调 Ollama，避免一次卡 20 秒。
    candidate_ids = {
        it["id"] for items in group_breaking.values() for it in items
        if not _is_bwenews_item(it)
    }
    scores: dict = {}
    if candidate_ids:
        base_url, model_name = await get_group_llm(groups[0]["group_id"])
        ollama_ok = True
        by_id = {it["id"]: it for it in fresh}
        for item_id in sorted(candidate_ids):
            it = by_id.get(item_id)
            if it is None:
                continue
            if not ollama_ok:
                scores[item_id] = None
                continue
            s = await _score_importance(
                base_url, model_name, it.get("title", ""), it.get("summary", "")
            )
            if s is None:
                ollama_ok = False
            scores[item_id] = s
    today = datetime.now(CST).date().isoformat()
    now_iso = datetime.now(CST).isoformat(timespec="seconds")
    # 所有群里算出的突发条目 id（并集）；没发完的动态要留在库里下一轮再发，
    # 所以结尾只把“已发送”或“不是突发”的条目标记为已处理。
    all_breaking_ids = {
        it["id"] for items in group_breaking.values() for it in items
    }
    sent_ids: set = set()
    low_score_ids: set = set()
    for cfg in groups:
        group_id = cfg["group_id"]
        breaking = group_breaking.get(group_id) or []
        if not breaking:
            continue
        async with get_db() as db:
            # 每日上限只算 RSS 突发：动态、方程式不计入（pao 2026-11-13），
            # 所以这里按来源排除 Muse动态 和方程式新闻的发送记录。
            sent_today = await db.fetchrow(
                "SELECT COUNT(*) AS c FROM ai_news_sends s "
                "LEFT JOIN ai_news_items i ON s.item_id = i.id "
                "WHERE s.group_id = ? AND s.kind = 'breaking' "
                "AND substr(s.sent_at, 1, 10) = ? "
                "AND (i.source IS NULL OR (i.source NOT LIKE 'Muse%' "
                "AND i.source NOT LIKE '方程式%'))",
                (group_id, today),
            )
        already = int((sent_today or {}).get("c") or 0)
        # 每日上限只约束 RSS 突发；动态、方程式不计入、不占名额，
        # 动态只受每轮 2 条的限流，方程式直通不限流。
        rss_budget = _max_breaking_per_day(cfg) - already
        feed_sent_this_cycle = 0
        rss_sent_this_cycle = 0
        for it in breaking:
            # AI 打分：低分（< 4）只进早报，不发快讯，也不占预算/限流名额。
            # 打分失败（None）按关键词直接发；方程式新闻不打分、直通。
            if not _is_bwenews_item(it):
                s = scores.get(it["id"])
                if s is not None and s < IMPORTANCE_THRESHOLD:
                    low_score_ids.add(it["id"])
                    continue
            if _is_bwenews_item(it):
                pass
            elif _is_feed_item(it):
                # 动态快讯每轮限流：超过 2 条的留到下一轮，不丢。
                if feed_sent_this_cycle >= MAX_FEED_BREAKING_PER_CYCLE:
                    continue
                feed_sent_this_cycle += 1
            else:
                if rss_sent_this_cycle >= rss_budget:
                    continue
                rss_sent_this_cycle += 1
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
                await bot.send_message(chat_id=group_id, text=text, disable_web_page_preview=True)
                sent_ids.add(it["id"])
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
            if (
                it["id"] in sent_ids
                or it["id"] not in all_breaking_ids
                or it["id"] in low_score_ids
            ):
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
        # 循环每 5 分钟才跑一轮，不一定正好卡在 daily_time 那一分钟；
        # 改成“到点后第一次循环就发”，否则那一分钟没轮到就整天漏发。
        if now.strftime("%H:%M") < daily_time:
            continue
        if (cfg.get("last_digest_date") or "") == today_str:
            continue
        async with get_db() as db:
            items = await db.fetch(
                "SELECT * FROM ai_news_items "
                "WHERE (substr(fetched_at, 1, 10) = ? OR substr(published_at, 1, 10) = ?) AND source NOT LIKE '方程式%' "
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
            await bot.send_message(chat_id=group_id, text=digest, disable_web_page_preview=True)
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


async def send_test_digest(bot: Bot, group_id: int) -> dict:
    """手动跑一条昨日早报（测试用）：不看时间、不记 last_digest_date，可重复跑。

    返回 {"status": "ok"/"no_news", "date", "items", "fallback", "text"}。
    注意：和 send_daily_digest 里的取数/拼文案逻辑保持一致，改一处要同步另一处。
    """
    yesterday = datetime.now(CST).date() - timedelta(days=1)
    date_label = f"{yesterday.month}月{yesterday.day}日"
    async with get_db() as db:
        items = await db.fetch(
            "SELECT * FROM ai_news_items "
            "WHERE (substr(fetched_at, 1, 10) = ? OR substr(published_at, 1, 10) = ?) AND source NOT LIKE '方程式%' "
            "ORDER BY id DESC LIMIT 10",
            (yesterday.isoformat(), yesterday.isoformat()),
        )
    if not items:
        return {"status": "no_news", "date": date_label, "items": 0, "fallback": False, "text": ""}
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
    used_fallback = False
    if not digest:
        digest = build_fallback_digest(date_label, items)
        used_fallback = True
        logger.warning("测试早报 LLM 摘要失败 (group=%s)，已降级为标题清单", group_id)
    try:
        await bot.send_message(chat_id=group_id, text=digest, disable_web_page_preview=True)
        logger.info("测试早报已发 (group=%s, %s 条新闻)", group_id, len(items))
    except Exception as exc:
        logger.warning("测试早报发送失败 (group=%s): %s", group_id, exc)
        return {"status": "send_failed", "date": date_label, "items": len(items),
                "fallback": used_fallback, "text": digest}
    return {"status": "ok", "date": date_label, "items": len(items),
            "fallback": used_fallback, "text": digest}


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
