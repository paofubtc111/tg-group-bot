import asyncio
import logging
from typing import List, Optional, Tuple

import httpx
from aiogram import Bot, F, Router, types
from aiogram.enums import ChatAction

from app.bot.handlers.moderation import inspect_and_moderate_message
from app.config import settings
from app.database.db import get_db

logger = logging.getLogger(__name__)
router = Router(name="llm_chat_router")

# 全局防并发爆显存信号量 (PRD 3.5 & 7.4: 最大并发 2)
OLLAMA_SEMAPHORE = asyncio.Semaphore(settings.OLLAMA_MAX_CONCURRENCY)


def _check_llm_trigger(
    message: types.Message,
    bot_user: types.User,
    llm_cfg: dict,
) -> Tuple[bool, str]:
    """
    判定当前消息是否满足本地 Ollama 唤醒条件，并返回清洗后的用户提问文本
    """
    raw_text = (message.text or "").strip()
    if not raw_text or raw_text.startswith("/"):
        return False, ""

    # 私聊直接触发
    if message.chat.type == "private":
        return True, raw_text

    if not llm_cfg or not llm_cfg.get("enabled"):
        return False, ""

    bot_username = (bot_user.username or "").lower()

    # 1. 显式 @BotUsername 触发
    if llm_cfg.get("trigger_on_mention", True) and bot_username:
        mention_tag = f"@{bot_username}"
        if mention_tag in raw_text.lower():
            cleaned = raw_text
            for token in (f"@{bot_user.username}", mention_tag):
                cleaned = cleaned.replace(token, "").strip()
            return True, cleaned or "你好"

    # 2. 引用 (Reply) Bot 消息触发
    if (
        llm_cfg.get("trigger_on_reply", True)
        and message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_user.id
    ):
        return True, raw_text

    # 3. 关键词前缀触发 (逗号分隔，如 "ai ,小助手 ")
    keywords_raw = llm_cfg.get("trigger_keywords") or ""
    if keywords_raw:
        prefixes = [kw.strip() for kw in keywords_raw.split(",") if kw.strip()]
        lower_text = raw_text.lower()
        for prefix in prefixes:
            if lower_text.startswith(prefix.lower()):
                cleaned = raw_text[len(prefix) :].strip()
                return True, cleaned or "你好"

    return False, ""


async def _load_chat_context(
    group_id: int, user_id: int, context_mode: str, context_limit: int
) -> List[dict]:
    """
    从 SQLite 加载最近 N 轮历史对话上下文
    """
    limit_rows = max(2, int(context_limit or 6) * 2)
    async with get_db() as db:
        if context_mode == "shared":
            rows = await db.fetch(
                """
                SELECT role, content FROM llm_chat_history
                WHERE group_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (group_id, limit_rows),
            )
        else:
            rows = await db.fetch(
                """
                SELECT role, content FROM llm_chat_history
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (group_id, user_id, limit_rows),
            )
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def _save_chat_turn(
    group_id: int, user_id: int, user_prompt: str, assistant_reply: str
) -> None:
    async with get_db() as db:
        await db.executemany(
            """
            INSERT INTO llm_chat_history (group_id, user_id, role, content)
            VALUES (?, ?, ?, ?)
            """,
            [
                (group_id, user_id, "user", user_prompt),
                (group_id, user_id, "assistant", assistant_reply),
            ],
        )


async def query_ollama_chat(
    base_url: str,
    model_name: str,
    system_prompt: str,
    history: List[dict],
    user_prompt: str,
) -> Optional[str]:
    """
    在全局 asyncio.Semaphore(2) 保护下请求本地 Ollama /api/chat 接口
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    endpoint = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
    }

    async with OLLAMA_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
                resp = await client.post(endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("message") or {}).get("content", "").strip()
                return content or None
        except Exception as exc:
            logger.warning("请求本地 Ollama 失败 (%s): %s", endpoint, exc)
            return None


@router.message(F.text | F.caption | F.forward_origin)
async def group_message_pipeline(message: types.Message, bot: Bot) -> None:
    """
    群组与私聊消息统一管道：
    1. 先执行内容安全拦截与阶梯处罚 (moderation)
    2. 若未违规且满足唤醒条件，则路由至本地 Ollama 大模型对话
    """
    # 第一步：群聊安全审计拦截
    if message.chat.type in ("group", "supergroup"):
        violated = await inspect_and_moderate_message(message, bot)
        if violated:
            return

    if not message.text or not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    async with get_db() as db:
        llm_cfg = await db.fetchrow(
            "SELECT * FROM group_llm_settings WHERE group_id = ?", (chat_id,)
        )

    # 私聊使用默认 Ollama 配置
    if message.chat.type == "private" and not llm_cfg:
        llm_cfg = {
            "enabled": True,
            "base_url": settings.OLLAMA_BASE_URL,
            "model_name": "qwen2.5:7b",
            "system_prompt": "你是一个群组智能管家，性格幽默、有问必答、语气简短有力。",
            "trigger_on_mention": True,
            "trigger_on_reply": True,
            "trigger_keywords": "",
            "context_enabled": True,
            "context_mode": "user_isolated",
            "context_limit": 6,
        }

    bot_user = await bot.get_me()
    triggered, prompt_text = _check_llm_trigger(message, bot_user, llm_cfg or {})
    if not triggered or not prompt_text:
        return

    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    base_url = (llm_cfg.get("base_url") if llm_cfg else None) or settings.OLLAMA_BASE_URL
    model_name = (llm_cfg.get("model_name") if llm_cfg else None) or "qwen2.5:7b"
    system_prompt = (
        llm_cfg.get("system_prompt")
        if llm_cfg
        else "你是一个群组智能管家，性格幽默、有问必答、语气简短有力。"
    )
    context_enabled = bool(llm_cfg.get("context_enabled", True)) if llm_cfg else True
    context_mode = (llm_cfg.get("context_mode") if llm_cfg else "user_isolated") or "user_isolated"
    context_limit = int(llm_cfg.get("context_limit") or 6) if llm_cfg else 6

    history: List[dict] = []
    if context_enabled:
        history = await _load_chat_context(chat_id, user_id, context_mode, context_limit)

    reply_text = await query_ollama_chat(
        base_url=base_url,
        model_name=model_name,
        system_prompt=system_prompt,
        history=history,
        user_prompt=prompt_text,
    )

    if not reply_text:
        await message.reply(
            "🤖 本地 AI 引擎当前繁忙或暂未就绪（请确认 Ollama 服务与模型已启动），请稍后再试。"
        )
        return

    if context_enabled:
        await _save_chat_turn(chat_id, user_id, prompt_text, reply_text)

    await message.reply(reply_text)
