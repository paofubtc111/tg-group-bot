import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiogram.types import ChatPermissions
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.auth import (
    check_user_is_group_admin,
    get_current_user,
    require_group_admin,
)
from app.bot.handlers.captcha import UNRESTRICTED_PERMISSIONS
from app.config import settings
from app.database.db import (
    ensure_group_initialized,
    get_db,
    record_audit_log,
)

logger = logging.getLogger(__name__)
api_router = APIRouter()


# ==================== Pydantic 请求模型定义 ====================


class CaptchaConfigPayload(BaseModel):
    enabled: bool = True
    captcha_type: str = Field(default="math", pattern="^(click|math|choice)$")
    timeout: int = Field(default=60, ge=15, le=600)
    action_on_fail: str = Field(default="kick", pattern="^(kick|ban)$")
    auto_delete_prompt: bool = True


class ButtonItem(BaseModel):
    text: str
    url: str


class TemplatePayload(BaseModel):
    template_id: Optional[int] = None
    template_name: str
    media_type: str = Field(default="none", pattern="^(none|photo|animation)$")
    media_source: Optional[str] = ""
    content: str
    buttons: List[List[ButtonItem]] = Field(default_factory=list)


class WelcomeConfigPayload(BaseModel):
    enabled: bool = True
    active_template_id: Optional[int] = None
    delete_ttl: int = Field(default=120, ge=0, le=86400)
    clean_previous: bool = True


class SensitiveRuleItem(BaseModel):
    rule_id: Optional[int] = None
    pattern: str
    match_type: str = Field(default="contains", pattern="^(contains|exact|regex)$")
    punish_action: str = Field(
        default="delete_only", pattern="^(delete_only|mute|kick|ban)$"
    )
    mute_duration: int = Field(default=3600, ge=60, le=2592000)


class ModerationConfigPayload(BaseModel):
    block_tg_links: bool = True
    block_all_urls: bool = False
    block_forwards: bool = False
    send_warning_msg: bool = True
    warning_ttl: int = Field(default=5, ge=0, le=300)
    max_warn_count: int = Field(default=3, ge=1, le=20)
    warn_action: str = Field(default="kick", pattern="^(kick|ban|mute)$")
    moderate_admins: bool = False
    rules: Optional[List[SensitiveRuleItem]] = None
    batch_keywords: Optional[List[str]] = None


class LLMConfigPayload(BaseModel):
    enabled: bool = False
    base_url: str = "http://host.docker.internal:11434"
    model_name: str = "qwen2.5:7b"
    system_prompt: str = "你是一个群组智能管家，性格幽默、有问必答、语气简短有力。"
    trigger_on_mention: bool = True
    trigger_on_reply: bool = True
    trigger_keywords: str = ""
    context_enabled: bool = True
    context_mode: str = Field(
        default="user_isolated", pattern="^(shared|user_isolated)$"
    )
    context_limit: int = Field(default=6, ge=1, le=30)


class FeedImportPayload(BaseModel):
    """Muse 动态导入：一批单元，每条 {title, body_md, source, published_at, unit_id}。"""
    units: List[Dict[str, Any]] = Field(default_factory=list)


class AINewsConfigPayload(BaseModel):
    daily_enabled: bool = False
    daily_time: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    breaking_enabled: bool = False
    # 突发关键词三档（逗号分隔；空串 = 用默认词）
    breaking_high_signal: str = ""
    breaking_models: str = ""
    breaking_events: str = ""
    # 每日突发上限（1-10，默认 3）
    max_breaking_per_day: int = Field(default=50, ge=1, le=50)


class EmergencyConfigPayload(BaseModel):
    lockdown_mode: bool = False
    raid_protection: bool = False
    enable_admin_call: bool = True
    notify_admins_dm: bool = False


class WhitelistPayload(BaseModel):
    user_id: int
    username: str = ""
    note: str = ""


class MemberActionPayload(BaseModel):
    action: str = Field(pattern="^(mute|unmute|kick|ban|unban)$")
    duration: int = Field(default=3600, ge=60, le=2592000)  # 秒


class BackfillMemberPayload(BaseModel):
    user_id: int


class KeywordReplyPayload(BaseModel):
    reply_id: Optional[int] = None
    keyword: str
    match_type: str = Field(default="contains", pattern="^(contains|exact)$")
    reply_text: str
    enabled: bool = True


class ScheduledPayload(BaseModel):
    id: Optional[int] = None
    content: str
    send_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    enabled: bool = True


class ForceSubscribePayload(BaseModel):
    enabled: bool = False
    channel: str = ""
    prompt_text: str = "请先加入指定频道后再发言。"


# ---- Batch 2: 积分系统 ----
class PointRulePayload(BaseModel):
    rule_id: Optional[int] = None
    trigger_action: str = Field(pattern="^(first_message|every_message|random_message)$")
    points: int = Field(default=1, ge=1, le=10000)
    chance: int = Field(default=100, ge=1, le=100)
    enabled: bool = True


class PointSettingsPayload(BaseModel):
    alias: str = "积分"
    daily_max: int = Field(default=0, ge=0, le=100000)
    decimals: bool = False
    auto_clear_inactive: bool = False
    signin_points: int = Field(default=10, ge=0, le=10000)
    streak_bonus_points: int = Field(default=5, ge=0, le=10000)
    enabled: bool = True


class PointRedemptionPayload(BaseModel):
    id: Optional[int] = None
    keyword: str
    cost: int = Field(default=0, ge=0, le=10000000)
    description: str = ""
    enabled: bool = True


# ---- Batch 2: 抽奖与游戏 ----
class LotteryPrizeItem(BaseModel):
    prize_name: str
    total: int = Field(default=1, ge=1, le=10000)


class LotteryPayload(BaseModel):
    id: Optional[int] = None
    lottery_type: str = Field(
        pattern="^(msg_count|msg_rank|signin_count|invite_count|points_scratch|points_timed)$"
    )
    title: str
    keyword: str = "抽奖"
    draw_keyword: str = "开奖"
    start_at: Optional[str] = ""
    end_at: Optional[str] = ""
    threshold: int = Field(default=0, ge=0, le=1000000)
    draw_mode: str = Field(default="full", pattern="^(full|timed)$")
    cost_per_draw: int = Field(default=0, ge=0, le=10000000)
    max_draws_per_user: int = Field(default=1, ge=1, le=1000)
    status: str = Field(default="active", pattern="^(active|ended)$")
    prizes: Optional[List[LotteryPrizeItem]] = None


class GuessOptionItem(BaseModel):
    option_text: str
    odds: float = Field(default=2.0, ge=1.0, le=100.0)


class GuessPayload(BaseModel):
    id: Optional[int] = None
    title: str
    question: str
    cost_per_bet: int = Field(default=10, ge=0, le=10000000)
    odds_mode: str = Field(default="prize", pattern="^(prize|odds)$")
    prize_pool: int = Field(default=0, ge=0, le=100000000)
    prize_amount: int = Field(default=0, ge=0, le=100000000)
    options: Optional[List[GuessOptionItem]] = None


class GuessClosePayload(BaseModel):
    winning_option_id: int


class DiceSettingsPayload(BaseModel):
    enabled: bool = False
    cost_per_round: int = Field(default=10, ge=0, le=10000000)
    payout_multiplier: float = Field(default=2.0, ge=1.0, le=100.0)
    house_cut_percent: int = Field(default=0, ge=0, le=100)


# ---- Batch 2: 其余模块 ----
class RotatingAdPayload(BaseModel):
    id: Optional[int] = None
    image_url: str = ""
    link_url: str = ""
    text: str = ""
    enabled: bool = True


class RotatingAdsSettingsPayload(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    display_seconds: int = Field(default=0, ge=0, le=86400)


class ScheduledLockdownPayload(BaseModel):
    id: Optional[int] = None
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    enabled: bool = True


class LockdownKeywordPayload(BaseModel):
    id: Optional[int] = None
    keyword: str
    lock_minutes: int = Field(default=30, ge=1, le=43200)
    enabled: bool = True
    allow_admin_trigger: bool = False


class TogglePayload(BaseModel):
    enabled: bool = False


class BottomButtonPayload(BaseModel):
    id: Optional[int] = None
    button_text: str
    reply_text: str
    enabled: bool = True


class MiscSettingsPayload(BaseModel):
    auto_delete_join: bool = True
    auto_delete_leave: bool = True
    auto_delete_pinned_notice: bool = True
    auto_delete_promotion: bool = False
    cancel_channel_pin: bool = False
    calculator: bool = False
    auto_check_u: bool = False


class GroupImportPayload(BaseModel):
    captcha: Optional[CaptchaConfigPayload] = None
    welcome: Optional[WelcomeConfigPayload] = None
    moderation: Optional[ModerationConfigPayload] = None
    llm: Optional[LLMConfigPayload] = None
    emergency: Optional[EmergencyConfigPayload] = None
    # 欢迎模板内容 (导出时带上; 导入时以副本形式新建并重映射 active_template_id)。
    # 只导设置不导模板内容的话, 导入到新库/新群后 active_template_id 会指向不存在的模板行。
    templates: Optional[List[TemplatePayload]] = None


# ==================== WebApp 首页与基础鉴权路由 ====================


@api_router.get("/", response_class=HTMLResponse)
async def serve_webapp_index() -> HTMLResponse:
    index_file = Path(settings.TEMPLATES_DIR) / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="WebApp 模板文件 index.html 不存在")
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@api_router.get("/api/health")
async def health_check() -> dict:
    return {"status": "ok", "service": "tg-group-bot"}


@api_router.post("/api/auth/verify")
async def api_verify_auth(user: dict = Depends(get_current_user)) -> dict:
    """
    校验 Telegram 签名，返回当前用户 ID 与姓名
    """
    return {
        "authenticated": True,
        "user": {
            "id": user.get("id"),
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
        },
    }


@api_router.get("/api/groups")
async def api_list_admin_groups(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """
    返回当前用户担任管理员的所有群组列表
    """
    user_id = int(user["id"])
    async with get_db() as db:
        all_groups = await db.fetch(
            "SELECT * FROM group_settings WHERE is_active = 1 ORDER BY updated_at DESC"
        )

    managed_groups = []
    for grp in all_groups:
        if await check_user_is_group_admin(request, grp["group_id"], user_id):
            managed_groups.append(grp)

    return {"groups": managed_groups}


# ==================== 1. 群组配置总览 ====================


@api_router.get("/api/group/{group_id}/overview")
async def api_group_overview(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        grp = await db.fetchrow(
            "SELECT * FROM group_settings WHERE group_id = ?", (group_id,)
        )
        captcha = await db.fetchrow(
            "SELECT * FROM group_captcha_settings WHERE group_id = ?", (group_id,)
        )
        welcome = await db.fetchrow(
            "SELECT * FROM group_welcome_settings WHERE group_id = ?", (group_id,)
        )
        moderation = await db.fetchrow(
            "SELECT * FROM group_moderation_settings WHERE group_id = ?", (group_id,)
        )
        llm = await db.fetchrow(
            "SELECT * FROM group_llm_settings WHERE group_id = ?", (group_id,)
        )
        emergency = await db.fetchrow(
            "SELECT * FROM group_emergency_settings WHERE group_id = ?", (group_id,)
        )
        rule_cnt = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM group_sensitive_rules WHERE group_id = ?",
            (group_id,),
        )
        tpl_cnt = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM message_templates WHERE group_id = ? OR group_id IS NULL",
            (group_id,),
        )
        warn_cnt = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM user_warn_records WHERE group_id = ?",
            (group_id,),
        )

    return {
        "group": grp,
        "modules": {
            "captcha_enabled": bool(captcha["enabled"]) if captcha else True,
            "captcha_type": captcha["captcha_type"] if captcha else "math",
            "welcome_enabled": bool(welcome["enabled"]) if welcome else True,
            "welcome_ttl": welcome["delete_ttl"] if welcome else 120,
            "block_tg_links": bool(moderation["block_tg_links"]) if moderation else True,
            "block_all_urls": bool(moderation["block_all_urls"]) if moderation else False,
            "sensitive_rules_count": rule_cnt["cnt"] if rule_cnt else 0,
            "templates_count": tpl_cnt["cnt"] if tpl_cnt else 0,
            "warned_users_count": warn_cnt["cnt"] if warn_cnt else 0,
            "llm_enabled": bool(llm["enabled"]) if llm else False,
            "llm_model": llm["model_name"] if llm else "qwen2.5:7b",
            "lockdown_mode": bool(emergency["lockdown_mode"]) if emergency else False,
            "raid_protection": bool(emergency["raid_protection"]) if emergency else False,
        },
    }


# ==================== 2. 入群人机验证 (CAPTCHA) ====================


@api_router.get("/api/group/{group_id}/captcha")
async def api_get_captcha(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        cfg = await db.fetchrow(
            "SELECT * FROM group_captcha_settings WHERE group_id = ?", (group_id,)
        )
    return {"captcha": cfg}


@api_router.post("/api/group/{group_id}/captcha")
async def api_save_captcha(
    group_id: int,
    payload: CaptchaConfigPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        await db.execute(
            """
            UPDATE group_captcha_settings
            SET enabled = ?, captcha_type = ?, timeout = ?, action_on_fail = ?, auto_delete_prompt = ?
            WHERE group_id = ?
            """,
            (
                int(payload.enabled),
                payload.captcha_type,
                payload.timeout,
                payload.action_on_fail,
                int(payload.auto_delete_prompt),
                group_id,
            ),
        )
        updated = await db.fetchrow(
            "SELECT * FROM group_captcha_settings WHERE group_id = ?", (group_id,)
        )

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or user.get("username") or "Admin",
        "update_captcha",
        f"更新入群验证: 开启={payload.enabled}, 模式={payload.captcha_type}, 超时={payload.timeout}s, 失败动作={payload.action_on_fail}",
    )
    return {"status": "ok", "captcha": updated}


# ==================== 3. 独立消息模板库 ====================


@api_router.get("/api/group/{group_id}/templates")
async def api_get_templates(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        rows = await db.fetch(
            """
            SELECT * FROM message_templates
            WHERE group_id = ? OR group_id IS NULL
            ORDER BY group_id DESC, template_id DESC
            """,
            (group_id,),
        )
    for r in rows:
        try:
            r["buttons"] = json.loads(r.get("buttons_json") or "[]")
        except Exception:
            r["buttons"] = []
    return {"templates": rows}


@api_router.post("/api/group/{group_id}/templates")
async def api_save_template(
    group_id: int,
    payload: TemplatePayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    buttons_json = json.dumps(
        [[b.model_dump() for b in row] for row in payload.buttons],
        ensure_ascii=False,
    )

    async with get_db() as db:
        if payload.template_id:
            await db.execute(
                """
                UPDATE message_templates
                SET template_name = ?, media_type = ?, media_source = ?, content = ?, buttons_json = ?
                WHERE template_id = ? AND (group_id = ? OR group_id IS NULL)
                """,
                (
                    payload.template_name,
                    payload.media_type,
                    payload.media_source or "",
                    payload.content,
                    buttons_json,
                    payload.template_id,
                    group_id,
                ),
            )
            tid = payload.template_id
            action_label = "更新消息模板"
        else:
            tid = await db.execute(
                """
                INSERT INTO message_templates (group_id, template_name, media_type, media_source, content, buttons_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    payload.template_name,
                    payload.media_type,
                    payload.media_source or "",
                    payload.content,
                    buttons_json,
                ),
            )
            action_label = "新建消息模板"

        tpl = await db.fetchrow(
            "SELECT * FROM message_templates WHERE template_id = ?", (tid,)
        )

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_template",
        f"{action_label}: {payload.template_name} (ID={tid})",
    )
    return {"status": "ok", "template": tpl}


@api_router.delete("/api/group/{group_id}/templates/{tid}")
async def api_delete_template(
    group_id: int,
    tid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM message_templates WHERE template_id = ? AND group_id = ?",
            (tid, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_template",
        f"删除消息模板 ID={tid}",
    )
    return {"status": "ok", "deleted_id": tid}


# ==================== 4. 入群欢迎配置 ====================


@api_router.get("/api/group/{group_id}/welcome")
async def api_get_welcome(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        welcome = await db.fetchrow(
            "SELECT * FROM group_welcome_settings WHERE group_id = ?", (group_id,)
        )
    return {"welcome": welcome}


@api_router.post("/api/group/{group_id}/welcome")
async def api_save_welcome(
    group_id: int,
    payload: WelcomeConfigPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        await db.execute(
            """
            UPDATE group_welcome_settings
            SET enabled = ?, active_template_id = ?, delete_ttl = ?, clean_previous = ?
            WHERE group_id = ?
            """,
            (
                int(payload.enabled),
                payload.active_template_id,
                payload.delete_ttl,
                int(payload.clean_previous),
                group_id,
            ),
        )
        welcome = await db.fetchrow(
            "SELECT * FROM group_welcome_settings WHERE group_id = ?", (group_id,)
        )

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_welcome",
        f"更新入群欢迎: 开启={payload.enabled}, 模板ID={payload.active_template_id}, TTL={payload.delete_ttl}s",
    )
    return {"status": "ok", "welcome": welcome}


# ==================== 5. 内容安全审计与敏感词 ====================


@api_router.get("/api/group/{group_id}/moderation")
async def api_get_moderation(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        settings_row = await db.fetchrow(
            "SELECT * FROM group_moderation_settings WHERE group_id = ?", (group_id,)
        )
        rules = await db.fetch(
            "SELECT * FROM group_sensitive_rules WHERE group_id = ? ORDER BY rule_id DESC",
            (group_id,),
        )
        warns = await db.fetch(
            "SELECT * FROM user_warn_records WHERE group_id = ? ORDER BY last_violation_time DESC LIMIT 50",
            (group_id,),
        )
    return {"moderation": settings_row, "rules": rules, "warn_records": warns}


@api_router.post("/api/group/{group_id}/moderation")
async def api_save_moderation(
    group_id: int,
    payload: ModerationConfigPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        await db.execute(
            """
            UPDATE group_moderation_settings
            SET block_tg_links = ?, block_all_urls = ?, block_forwards = ?,
                send_warning_msg = ?, warning_ttl = ?, max_warn_count = ?, warn_action = ?,
                moderate_admins = ?
            WHERE group_id = ?
            """,
            (
                int(payload.block_tg_links),
                int(payload.block_all_urls),
                int(payload.block_forwards),
                int(payload.send_warning_msg),
                payload.warning_ttl,
                payload.max_warn_count,
                payload.warn_action,
                int(payload.moderate_admins),
                group_id,
            ),
        )

        # 若传入了非空规则列表, 则全量同步更新。
        # 空列表视为“未改动规则” (保留现有规则), 避免面板保存一次空列表就清空全部规则;
        # 单条规则删除仍走单删接口。
        if payload.rules:
            await db.execute(
                "DELETE FROM group_sensitive_rules WHERE group_id = ?", (group_id,)
            )
            for item in payload.rules:
                if item.pattern.strip():
                    await db.execute(
                        """
                        INSERT INTO group_sensitive_rules (group_id, pattern, match_type, punish_action, mute_duration)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            item.pattern.strip(),
                            item.match_type,
                            item.punish_action,
                            item.mute_duration,
                        ),
                    )

        # 若传入了批量导入关键词数组，则追加插入
        if payload.batch_keywords:
            for kw in payload.batch_keywords:
                cleaned = kw.strip()
                if cleaned:
                    await db.execute(
                        """
                        INSERT INTO group_sensitive_rules (group_id, pattern, match_type, punish_action, mute_duration)
                        VALUES (?, ?, 'contains', 'delete_only', 3600)
                        """,
                        (group_id, cleaned),
                    )

        settings_row = await db.fetchrow(
            "SELECT * FROM group_moderation_settings WHERE group_id = ?", (group_id,)
        )
        rules = await db.fetch(
            "SELECT * FROM group_sensitive_rules WHERE group_id = ? ORDER BY rule_id DESC",
            (group_id,),
        )

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_moderation",
        f"更新内容审计策略 (当前敏感词数: {len(rules)})",
    )
    return {"status": "ok", "moderation": settings_row, "rules": rules}


# ==================== 6. 本地 Ollama AI 配置 ====================


@api_router.get("/api/group/{group_id}/llm")
async def api_get_llm(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        llm = await db.fetchrow(
            "SELECT * FROM group_llm_settings WHERE group_id = ?", (group_id,)
        )
        history_cnt = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM llm_chat_history WHERE group_id = ?",
            (group_id,),
        )
    return {"llm": llm, "history_count": history_cnt["cnt"] if history_cnt else 0}


@api_router.post("/api/group/{group_id}/llm")
async def api_save_llm(
    group_id: int,
    payload: LLMConfigPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        await db.execute(
            """
            UPDATE group_llm_settings
            SET enabled = ?, base_url = ?, model_name = ?, system_prompt = ?,
                trigger_on_mention = ?, trigger_on_reply = ?, trigger_keywords = ?,
                context_enabled = ?, context_mode = ?, context_limit = ?
            WHERE group_id = ?
            """,
            (
                int(payload.enabled),
                payload.base_url.strip(),
                payload.model_name.strip(),
                payload.system_prompt.strip(),
                int(payload.trigger_on_mention),
                int(payload.trigger_on_reply),
                payload.trigger_keywords.strip(),
                int(payload.context_enabled),
                payload.context_mode,
                payload.context_limit,
                group_id,
            ),
        )
        llm = await db.fetchrow(
            "SELECT * FROM group_llm_settings WHERE group_id = ?", (group_id,)
        )

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_llm",
        f"更新 Ollama AI 配置: 开启={payload.enabled}, 模型={payload.model_name}, 记忆模式={payload.context_mode}",
    )
    return {"status": "ok", "llm": llm}


@api_router.delete("/api/group/{group_id}/llm/history")
async def api_clear_llm_history(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM llm_chat_history WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "clear_llm_history",
        "清空本群 AI 历史对话上下文记忆",
    )
    return {"status": "ok"}


@api_router.get("/api/group/{group_id}/ai-news")
async def api_get_ai_news(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM ai_news_settings WHERE group_id = ?", (group_id,)
        )
        count = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM ai_news_items"
        )
    settings = dict(row) if row else {
        "group_id": group_id,
        "daily_enabled": 0,
        "daily_time": "09:00",
        "breaking_enabled": 0,
        "last_digest_date": "",
    }
    return {"ai_news": settings, "news_items_count": count["cnt"] if count else 0}


@api_router.post("/api/group/{group_id}/ai-news")
async def api_save_ai_news(
    group_id: int,
    payload: AINewsConfigPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO ai_news_settings "
            "(group_id, daily_enabled, daily_time, breaking_enabled, "
            "breaking_high_signal, breaking_models, breaking_events, "
            "max_breaking_per_day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (group_id, int(payload.daily_enabled), payload.daily_time.strip(),
             int(payload.breaking_enabled),
             payload.breaking_high_signal.strip(),
             payload.breaking_models.strip(),
             payload.breaking_events.strip(),
             payload.max_breaking_per_day),
        )
        await db.execute(
            "UPDATE ai_news_settings SET daily_enabled = ?, daily_time = ?, "
            "breaking_enabled = ?, breaking_high_signal = ?, "
            "breaking_models = ?, breaking_events = ?, "
            "max_breaking_per_day = ? WHERE group_id = ?",
            (int(payload.daily_enabled), payload.daily_time.strip(),
             int(payload.breaking_enabled),
             payload.breaking_high_signal.strip(),
             payload.breaking_models.strip(),
             payload.breaking_events.strip(),
             payload.max_breaking_per_day, group_id),
        )
        row = await db.fetchrow(
            "SELECT * FROM ai_news_settings WHERE group_id = ?", (group_id,)
        )

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_ai_news",
        f"更新 AI 新闻配置: 早报={payload.daily_enabled} {payload.daily_time}, 突发={payload.breaking_enabled}",
    )
    return {"status": "ok", "ai_news": row}


@api_router.post("/api/group/{group_id}/ai-news/test-digest")
async def api_test_digest(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="机器人未启动，稍后再试")
    from app.bot.handlers.ai_news import send_test_digest

    result = await send_test_digest(bot, group_id)
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "test_ai_news_digest",
        f"手动发送测试早报: {result.get('status')}, {result.get('items', 0)} 条新闻",
    )
    return result


@api_router.post("/api/ai-news/feed-import")
async def api_feed_import(payload: FeedImportPayload, request: Request) -> dict:
    """Muse 动态 → AI 新闻入库（供定时同步任务调用）。
    鉴权：请求头 X-Feed-Import-Token 须等于环境变量 FEED_IMPORT_TOKEN。
    不走 Telegram 签名，也不走群管理员校验；入库后走现有的突发/早报逻辑。"""
    token = settings.FEED_IMPORT_TOKEN
    if not token:
        raise HTTPException(status_code=503, detail="未配置 FEED_IMPORT_TOKEN")
    got = request.headers.get("X-Feed-Import-Token", "")
    if not got or got != token:
        raise HTTPException(status_code=401, detail="token 不正确")
    from app.bot.handlers.ai_news import import_feed_units

    result = await import_feed_units(payload.units)
    return {"status": "ok", **result}


@api_router.get("/api/admin/groups")
async def api_admin_groups(request: Request) -> dict:
    """小花群管后台群组同步专用只读接口（供后台定时/手动同步调用）。
    鉴权：请求头 X-Admin-Token 须等于环境变量 ADMIN_SYNC_TOKEN。
    不走 Telegram WebApp 签名；只读 group_settings 全表，不写库。
    Token 不正确或未配置时分别返回 401 / 503，token 本身不记日志。"""
    token = settings.ADMIN_SYNC_TOKEN
    if not token:
        raise HTTPException(status_code=503, detail="未配置 ADMIN_SYNC_TOKEN")
    got = request.headers.get("X-Admin-Token", "")
    if not got or got != token:
        raise HTTPException(status_code=401, detail="token 不正确")
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT group_id, group_title, is_active, added_by_user_id, "
            "added_by_username, created_at, updated_at FROM group_settings "
            "ORDER BY group_id"
        )
    groups = []
    for row in rows:
        created_at = row.get("created_at")
        updated_at = row.get("updated_at")
        groups.append(
            {
                "group_id": row["group_id"],
                "group_title": row["group_title"],
                "is_active": bool(row["is_active"]),
                "added_by_user_id": row.get("added_by_user_id"),
                "added_by_username": row.get("added_by_username"),
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
                "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
            }
        )
    return {"status": "ok", "groups": groups}


# ==================== 7. 紧急模式与权限 / 操作日志 / 导入导出 ====================


@api_router.get("/api/group/{group_id}/emergency")
async def api_get_emergency(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        emg = await db.fetchrow(
            "SELECT * FROM group_emergency_settings WHERE group_id = ?", (group_id,)
        )
    return {"emergency": emg}


@api_router.post("/api/group/{group_id}/emergency")
async def api_save_emergency(
    group_id: int,
    payload: EmergencyConfigPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        await db.execute(
            """
            UPDATE group_emergency_settings
            SET lockdown_mode = ?, raid_protection = ?, enable_admin_call = ?, notify_admins_dm = ?
            WHERE group_id = ?
            """,
            (
                int(payload.lockdown_mode),
                int(payload.raid_protection),
                int(payload.enable_admin_call),
                int(payload.notify_admins_dm),
                group_id,
            ),
        )
        emg = await db.fetchrow(
            "SELECT * FROM group_emergency_settings WHERE group_id = ?", (group_id,)
        )

    # 若 Bot 实例在线，同步尝试调用 Telegram set_chat_permissions 控制全员禁言
    bot = getattr(request.app.state, "bot", None)
    if bot:
        try:
            perms = ChatPermissions(can_send_messages=not payload.lockdown_mode)
            await bot.set_chat_permissions(chat_id=group_id, permissions=perms)
        except Exception as exc:
            logger.warning("同步设置群组全员权限失败: %s", exc)

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_emergency",
        f"更新紧急模式: 全员锁群={payload.lockdown_mode}, 防爆群={payload.raid_protection}",
    )
    return {"status": "ok", "emergency": emg}


@api_router.get("/api/group/{group_id}/logs")
async def api_get_audit_logs(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        logs = await db.fetch(
            "SELECT * FROM admin_audit_logs WHERE group_id = ? ORDER BY log_id DESC LIMIT 100",
            (group_id,),
        )
    return {"logs": logs}


@api_router.get("/api/group/{group_id}/export")
async def api_export_group_config(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    async with get_db() as db:
        captcha = await db.fetchrow(
            "SELECT enabled, captcha_type, timeout, action_on_fail, auto_delete_prompt FROM group_captcha_settings WHERE group_id = ?",
            (group_id,),
        )
        welcome = await db.fetchrow(
            "SELECT enabled, active_template_id, delete_ttl, clean_previous FROM group_welcome_settings WHERE group_id = ?",
            (group_id,),
        )
        moderation = await db.fetchrow(
            "SELECT block_tg_links, block_all_urls, block_forwards, send_warning_msg, warning_ttl, max_warn_count, warn_action, moderate_admins FROM group_moderation_settings WHERE group_id = ?",
            (group_id,),
        )
        rules = await db.fetch(
            "SELECT pattern, match_type, punish_action, mute_duration FROM group_sensitive_rules WHERE group_id = ?",
            (group_id,),
        )
        # 欢迎模板内容也要导出 (只导 active_template_id 的话, 导入到新库/新群后
        # 该 ID 指向不存在的模板行, 欢迎语就丢了)。导本群模板 + 当前激活模板
        # (激活的可能是系统全局模板), 导入时以副本形式新建并重映射 ID。
        active_tpl_id = welcome["active_template_id"] if welcome else None
        tpl_rows = await db.fetch(
            """
            SELECT template_id, template_name, media_type, media_source, content, buttons_json
            FROM message_templates WHERE group_id = ? OR template_id = ?
            """,
            (group_id, active_tpl_id),
        )
        templates = []
        seen_tpl_ids = set()
        for t in tpl_rows:
            if t["template_id"] in seen_tpl_ids:
                continue
            seen_tpl_ids.add(t["template_id"])
            try:
                buttons = json.loads(t.get("buttons_json") or "[]")
            except (ValueError, TypeError):
                buttons = []
            templates.append(
                {
                    "template_id": t["template_id"],
                    "template_name": t["template_name"],
                    "media_type": t["media_type"],
                    "media_source": t["media_source"] or "",
                    "content": t["content"],
                    "buttons": buttons,
                }
            )
        llm = await db.fetchrow(
            "SELECT enabled, base_url, model_name, system_prompt, trigger_on_mention, trigger_on_reply, trigger_keywords, context_enabled, context_mode, context_limit FROM group_llm_settings WHERE group_id = ?",
            (group_id,),
        )
        emergency = await db.fetchrow(
            "SELECT lockdown_mode, raid_protection, enable_admin_call, notify_admins_dm FROM group_emergency_settings WHERE group_id = ?",
            (group_id,),
        )

    if moderation is not None:
        moderation["rules"] = rules

    return {
        "version": "1.1",
        "source_group_id": group_id,
        "captcha": captcha,
        "welcome": welcome,
        "moderation": moderation,
        "llm": llm,
        "emergency": emergency,
        "templates": templates,
    }


@api_router.post("/api/group/{group_id}/import")
async def api_import_group_config(
    group_id: int,
    payload: GroupImportPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)

    # 欢迎模板先导入 (以副本形式新建, 不覆盖原模板行), 再把 welcome 的
    # active_template_id 重映射到新副本 ID, 否则欢迎设置会指向不存在的模板。
    if payload.templates:
        id_map: dict[int, int] = {}
        for tpl in payload.templates:
            copied = TemplatePayload(
                template_id=None,
                template_name=tpl.template_name,
                media_type=tpl.media_type,
                media_source=tpl.media_source or "",
                content=tpl.content,
                buttons=tpl.buttons or [],
            )
            saved = await api_save_template(group_id, copied, request, user)
            new_id = (saved.get("template") or {}).get("template_id")
            if tpl.template_id and new_id:
                id_map[tpl.template_id] = new_id
        if payload.welcome and payload.welcome.active_template_id in id_map:
            payload.welcome.active_template_id = id_map[
                payload.welcome.active_template_id
            ]

    if payload.captcha:
        await api_save_captcha(group_id, payload.captcha, request, user)
    if payload.welcome:
        await api_save_welcome(group_id, payload.welcome, request, user)
    if payload.moderation:
        # 导入是全量覆盖语义: 导出文件里 rules=[] 表示源群没有规则,
        # 必须清空目标群规则。面板保存路径把 [] 当“未改动” (Bug 5),
        # 所以这里先显式清空, 再走保存接口。
        if payload.moderation.rules is not None and len(payload.moderation.rules) == 0:
            async with get_db() as db:
                await db.execute(
                    "DELETE FROM group_sensitive_rules WHERE group_id = ?",
                    (group_id,),
                )
        await api_save_moderation(group_id, payload.moderation, request, user)
    if payload.llm:
        await api_save_llm(group_id, payload.llm, request, user)
    if payload.emergency:
        await api_save_emergency(group_id, payload.emergency, request, user)

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "import_config",
        "通过 JSON 导入并覆盖群组全量配置",
    )
    return {"status": "ok"}


# ==================== 8. 广告白名单用户 ====================


@api_router.get("/api/group/{group_id}/whitelist")
async def api_get_whitelist(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM group_whitelist WHERE group_id = ? ORDER BY created_at DESC",
            (group_id,),
        )
    return {"whitelist": rows}


@api_router.post("/api/group/{group_id}/whitelist")
async def api_add_whitelist(
    group_id: int,
    payload: WhitelistPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_whitelist (group_id, user_id, username, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                username = excluded.username, note = excluded.note
            """,
            (
                group_id,
                payload.user_id,
                (payload.username or "").strip().lstrip("@"),
                payload.note.strip(),
            ),
        )
        rows = await db.fetch(
            "SELECT * FROM group_whitelist WHERE group_id = ? ORDER BY created_at DESC",
            (group_id,),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "whitelist_add",
        f"添加广告白名单用户 {payload.user_id} (@{payload.username or ''})",
    )
    return {"status": "ok", "whitelist": rows}


@api_router.delete("/api/group/{group_id}/whitelist/{user_id}")
async def api_delete_whitelist(
    group_id: int,
    user_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM group_whitelist WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "whitelist_remove",
        f"移除广告白名单用户 {user_id}",
    )
    return {"status": "ok", "deleted_id": user_id}


# ==================== 9. 群成员管理 ====================


@api_router.get("/api/group/{group_id}/members")
async def api_get_members(
    group_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    page = max(1, page)
    page_size = min(100, max(1, page_size))

    # 通过 Telegram 实时拉取管理员列表, 同步 is_admin 标记;
    # 管理员 upsert 进 group_members (补 username/first_name), 至少保证全部管理员必在表里
    bot = getattr(request.app.state, "bot", None)
    admin_ids: set = set()
    if bot is not None:
        try:
            admins = await bot.get_chat_administrators(group_id)
            admin_ids = {adm.user.id for adm in admins}
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            async with get_db() as db:
                await db.execute(
                    "UPDATE group_members SET is_admin = 0 WHERE group_id = ?",
                    (group_id,),
                )
                for adm in admins:
                    u = adm.user
                    await db.execute(
                        """
                        INSERT INTO group_members (group_id, user_id, username, first_name, is_admin, message_count, last_seen, joined_at)
                        VALUES (?, ?, ?, ?, 1, 0, ?, ?)
                        ON CONFLICT(group_id, user_id) DO UPDATE SET
                            username = excluded.username,
                            first_name = excluded.first_name,
                            is_admin = 1,
                            last_seen = excluded.last_seen
                        """,
                        (group_id, u.id, u.username or "", u.first_name or "", now, now),
                    )
        except Exception as exc:
            logger.warning("拉取群管理员列表失败: %s", exc)

    where = "group_id = ?"
    params: list = [group_id]
    query = (q or "").strip()
    if query:
        where += " AND (username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?)"
        params += [f"%{query}%", f"%{query}%", f"%{query}%"]

    async with get_db() as db:
        total_row = await db.fetchrow(
            f"SELECT COUNT(*) AS cnt FROM group_members WHERE {where}", params
        )
        rows = await db.fetch(
            f"""
            SELECT * FROM group_members WHERE {where}
            ORDER BY is_admin DESC, message_count DESC, last_seen DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, (page - 1) * page_size],
        )
    return {
        "members": rows,
        "total": total_row["cnt"] if total_row else 0,
        "page": page,
        "page_size": page_size,
        "admin_ids": sorted(admin_ids),
    }


@api_router.post("/api/group/{group_id}/members/backfill")
async def api_backfill_member(
    group_id: int,
    payload: BackfillMemberPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    """按 ID 手动补录成员 (唯一能补录沉默老成员的合法途径)。

    后端调 bot.get_chat_member 拿到人后写入 group_members; 不存在则 INSERT,
    存在则更新档案。不是“自动同步全部成员” (Bot API 没有拉取全部成员的接口)。
    """
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot 未在线，无法补录成员")
    try:
        member = await bot.get_chat_member(group_id, payload.user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404, detail=f"在群内找不到该用户: {exc}"
        )
    if member.status in ("left", "kicked"):
        raise HTTPException(
            status_code=400, detail="该用户不在群内（已退群或被移出）"
        )
    u = member.user
    if u.is_bot:
        raise HTTPException(status_code=400, detail="不能补录机器人账号")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    is_admin = 1 if member.status in ("administrator", "creator") else 0
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_members (group_id, user_id, username, first_name, is_admin, message_count, last_seen, joined_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                is_admin = excluded.is_admin,
                last_seen = excluded.last_seen
            """,
            (group_id, u.id, u.username or "", u.first_name or "", is_admin, now, now),
        )
        row = await db.fetchrow(
            "SELECT * FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, payload.user_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "backfill_member",
        f"手动补录成员: {u.full_name} ({u.id})",
    )
    return {"status": "ok", "member": row}


@api_router.post("/api/group/{group_id}/members/{user_id}/action")
async def api_member_action(
    group_id: int,
    user_id: int,
    payload: MemberActionPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot 未在线，无法执行群成员操作")

    action = payload.action
    try:
        if action == "mute":
            until_ts = int(datetime.now(timezone.utc).timestamp()) + payload.duration
            await bot.restrict_chat_member(
                group_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_ts,
            )
            label = f"禁言 {payload.duration // 60} 分钟"
        elif action == "unmute":
            await bot.restrict_chat_member(
                group_id, user_id, permissions=UNRESTRICTED_PERMISSIONS
            )
            label = "解除禁言"
        elif action == "kick":
            await bot.ban_chat_member(group_id, user_id)
            await bot.unban_chat_member(group_id, user_id)
            label = "移出群聊"
        elif action == "ban":
            await bot.ban_chat_member(group_id, user_id)
            label = "永久封禁"
        else:  # unban
            await bot.unban_chat_member(group_id, user_id)
            label = "解除封禁"
    except Exception as exc:
        logger.warning("执行群成员操作失败 (action=%s, user=%s): %s", action, user_id, exc)
        raise HTTPException(status_code=502, detail=f"Telegram 执行失败: {exc}")

    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        f"member_{action}",
        f"管理员对用户 {user_id} 执行{label}",
    )
    return {"status": "ok", "action": action, "user_id": user_id}


# ==================== 10. 群消息管理 ====================


@api_router.get("/api/group/{group_id}/messages")
async def api_get_group_messages(
    group_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    await require_group_admin(request, group_id, user)
    page = max(1, page)
    page_size = min(100, max(1, page_size))

    where = "group_id = ?"
    params: list = [group_id]
    query = (q or "").strip()
    if query:
        where += " AND (text LIKE ? OR username LIKE ? OR CAST(user_id AS TEXT) LIKE ?)"
        params += [f"%{query}%", f"%{query}%", f"%{query}%"]

    async with get_db() as db:
        total_row = await db.fetchrow(
            f"SELECT COUNT(*) AS cnt FROM group_messages WHERE {where}", params
        )
        rows = await db.fetch(
            f"""
            SELECT * FROM group_messages WHERE {where}
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            params + [page_size, (page - 1) * page_size],
        )
    return {
        "messages": rows,
        "total": total_row["cnt"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@api_router.delete("/api/group/{group_id}/messages/{message_id}")
async def api_delete_group_message(
    group_id: int,
    message_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    bot = getattr(request.app.state, "bot", None)
    if bot is not None:
        try:
            await bot.delete_message(group_id, message_id)
        except Exception as exc:
            logger.warning("Telegram 撤回群消息失败 (msg=%s): %s", message_id, exc)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM group_messages WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_group_message",
        f"管理员在面板撤回群消息 ID={message_id}",
    )
    return {"status": "ok", "deleted_id": message_id}


# ==================== 11. 关键词自动回复 ====================


@api_router.get("/api/group/{group_id}/keyword-replies")
async def api_get_keyword_replies(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM keyword_replies WHERE group_id = ? ORDER BY reply_id DESC",
            (group_id,),
        )
    return {"replies": rows}


@api_router.post("/api/group/{group_id}/keyword-replies")
async def api_save_keyword_reply(
    group_id: int,
    payload: KeywordReplyPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    keyword = payload.keyword.strip()
    reply_text = payload.reply_text.strip()
    if not keyword or not reply_text:
        raise HTTPException(status_code=400, detail="关键词与回复内容均不能为空")
    async with get_db() as db:
        if payload.reply_id:
            await db.execute(
                """
                UPDATE keyword_replies
                SET keyword = ?, match_type = ?, reply_text = ?, enabled = ?
                WHERE reply_id = ? AND group_id = ?
                """,
                (
                    keyword,
                    payload.match_type,
                    reply_text,
                    int(payload.enabled),
                    payload.reply_id,
                    group_id,
                ),
            )
            rid = payload.reply_id
        else:
            rid = await db.execute(
                """
                INSERT INTO keyword_replies (group_id, keyword, match_type, reply_text, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, keyword, payload.match_type, reply_text, int(payload.enabled)),
            )
        row = await db.fetchrow(
            "SELECT * FROM keyword_replies WHERE reply_id = ?", (rid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_keyword_reply",
        f"{'更新' if payload.reply_id else '新增'}关键词自动回复: {keyword} ({payload.match_type})",
    )
    return {"status": "ok", "reply": row}


@api_router.delete("/api/group/{group_id}/keyword-replies/{reply_id}")
async def api_delete_keyword_reply(
    group_id: int,
    reply_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM keyword_replies WHERE reply_id = ? AND group_id = ?",
            (reply_id, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_keyword_reply",
        f"删除关键词自动回复 ID={reply_id}",
    )
    return {"status": "ok", "deleted_id": reply_id}


# ==================== 12. 定时发送 ====================


@api_router.get("/api/group/{group_id}/scheduled")
async def api_get_scheduled(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM scheduled_messages WHERE group_id = ? ORDER BY send_time",
            (group_id,),
        )
    return {"scheduled": rows}


@api_router.post("/api/group/{group_id}/scheduled")
async def api_save_scheduled(
    group_id: int,
    payload: ScheduledPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="定时消息内容不能为空")
    hh, mm = payload.send_time.split(":")
    if int(hh) > 23 or int(mm) > 59:
        raise HTTPException(status_code=400, detail="发送时间格式无效 (应为 HH:MM)")
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE scheduled_messages
                SET content = ?, send_time = ?, enabled = ?, last_sent_date = NULL
                WHERE id = ? AND group_id = ?
                """,
                (content, payload.send_time, int(payload.enabled), payload.id, group_id),
            )
            sid = payload.id
        else:
            sid = await db.execute(
                """
                INSERT INTO scheduled_messages (group_id, content, send_time, enabled)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, content, payload.send_time, int(payload.enabled)),
            )
        row = await db.fetchrow(
            "SELECT * FROM scheduled_messages WHERE id = ?", (sid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_scheduled",
        f"{'更新' if payload.id else '新增'}定时发送任务: 每天 {payload.send_time} (ID={sid})",
    )
    return {"status": "ok", "scheduled": row}


@api_router.delete("/api/group/{group_id}/scheduled/{sid}")
async def api_delete_scheduled(
    group_id: int,
    sid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM scheduled_messages WHERE id = ? AND group_id = ?",
            (sid, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_scheduled",
        f"删除定时发送任务 ID={sid}",
    )
    return {"status": "ok", "deleted_id": sid}


# ==================== 13. 强制订阅 ====================


@api_router.get("/api/group/{group_id}/force-subscribe")
async def api_get_force_subscribe(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_force_subscribe WHERE group_id = ?", (group_id,)
        )
    if not row:
        row = {
            "group_id": group_id,
            "enabled": 0,
            "channel": "",
            "prompt_text": "请先加入指定频道后再发言。",
        }
    return {"force_subscribe": row}


@api_router.post("/api/group/{group_id}/force-subscribe")
async def api_save_force_subscribe(
    group_id: int,
    payload: ForceSubscribePayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    channel = (payload.channel or "").strip()
    if payload.enabled and not channel:
        raise HTTPException(status_code=400, detail="开启强制订阅前请先填写频道用户名或 ID")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_force_subscribe (group_id, enabled, channel, prompt_text)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                enabled = excluded.enabled,
                channel = excluded.channel,
                prompt_text = excluded.prompt_text
            """,
            (
                group_id,
                int(payload.enabled),
                channel,
                (payload.prompt_text or "").strip() or "请先加入指定频道后再发言。",
            ),
        )
        row = await db.fetchrow(
            "SELECT * FROM group_force_subscribe WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_force_subscribe",
        f"更新强制订阅: 开启={payload.enabled}, 频道={channel or '未设置'}",
    )
    return {"status": "ok", "force_subscribe": row}


# ==================== 14. 消息统计 ====================


@api_router.get("/api/group/{group_id}/stats")
async def api_get_group_stats(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    today_cst = datetime.now(timezone(timedelta(hours=8))).date()
    days = [today_cst - timedelta(days=i) for i in range(6, -1, -1)]

    async with get_db() as db:
        daily_rows = await db.fetch(
            """
            SELECT date(created_at, '+8 hours') AS day, COUNT(*) AS cnt
            FROM group_messages
            WHERE group_id = ?
              AND date(created_at, '+8 hours') >= date('now', '+8 hours', '-6 days')
            GROUP BY day ORDER BY day
            """,
            (group_id,),
        )
        top_rows = await db.fetch(
            """
            SELECT user_id, username, COUNT(*) AS cnt FROM group_messages
            WHERE group_id = ? GROUP BY user_id, username ORDER BY cnt DESC LIMIT 5
            """,
            (group_id,),
        )
        members_total = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM group_members WHERE group_id = ?", (group_id,)
        )
        total_msgs = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM group_messages WHERE group_id = ?", (group_id,)
        )
        deletions = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM admin_audit_logs WHERE group_id = ? AND action_type LIKE 'violation%'",
            (group_id,),
        )
        verifications = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM admin_audit_logs WHERE group_id = ? AND action_type LIKE 'captcha%'",
            (group_id,),
        )

    # PG 里 date(...) 返回 date 对象, SQLite 返回字符串; 统一成 'YYYY-MM-DD'
    # 字符串再当字典键, 否则 d.isoformat() 查不到, 每天计数全是 0。
    day_counts = {}
    for r in daily_rows:
        day_key = r["day"]
        if hasattr(day_key, "isoformat"):
            day_key = day_key.isoformat()
        day_counts[str(day_key)] = r["cnt"]
    daily = [
        {"day": d.isoformat(), "count": day_counts.get(d.isoformat(), 0)} for d in days
    ]
    return {
        "daily": daily,
        "top_senders": top_rows,
        "total_members": members_total["cnt"] if members_total else 0,
        "total_messages": total_msgs["cnt"] if total_msgs else 0,
        "deletions": deletions["cnt"] if deletions else 0,
        "verifications": verifications["cnt"] if verifications else 0,
    }


# ==================== Batch 2: 15. 积分系统 ====================


def _default_point_settings(group_id: int) -> dict:
    return {
        "group_id": group_id,
        "alias": "积分",
        "daily_max": 0,
        "decimals": 0,
        "auto_clear_inactive": 0,
        "signin_points": 10,
        "streak_bonus_points": 5,
        "enabled": 1,
    }


@api_router.get("/api/group/{group_id}/points")
async def api_get_points(
    group_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    # 注意: user_points 与 group_members 都有 group_id, WHERE 必须加 up. 前缀
    where = "up.group_id = ?"
    params: list = [group_id]
    query = (q or "").strip()
    if query:
        where += " AND CAST(up.user_id AS TEXT) LIKE ?"
        params += [f"%{query}%"]
    async with get_db() as db:
        total_row = await db.fetchrow(
            f"SELECT COUNT(*) AS cnt FROM user_points up WHERE {where}", params
        )
        rows = await db.fetch(
            f"""
            SELECT up.*, COALESCE(gm.username, '') AS username,
                   COALESCE(gm.first_name, '') AS first_name
            FROM user_points up
            LEFT JOIN group_members gm
              ON gm.group_id = up.group_id AND gm.user_id = up.user_id
            WHERE {where}
            ORDER BY up.balance DESC, up.total_earned DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, (page - 1) * page_size],
        )
    return {
        "points": rows,
        "total": total_row["cnt"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@api_router.get("/api/group/{group_id}/point-logs")
async def api_get_point_logs(
    group_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    await require_group_admin(request, group_id, user)
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where = "group_id = ?"
    params: list = [group_id]
    query = (q or "").strip()
    if query:
        where += " AND (CAST(user_id AS TEXT) LIKE ? OR reason LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    async with get_db() as db:
        total_row = await db.fetchrow(
            f"SELECT COUNT(*) AS cnt FROM point_logs WHERE {where}", params
        )
        rows = await db.fetch(
            f"""
            SELECT * FROM point_logs WHERE {where}
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            params + [page_size, (page - 1) * page_size],
        )
    return {
        "logs": rows,
        "total": total_row["cnt"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@api_router.get("/api/group/{group_id}/point-rules")
async def api_get_point_rules(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM point_rules WHERE group_id = ? ORDER BY rule_id",
            (group_id,),
        )
    return {"rules": rows}


@api_router.post("/api/group/{group_id}/point-rules")
async def api_save_point_rule(
    group_id: int,
    payload: PointRulePayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        if payload.rule_id:
            await db.execute(
                """
                UPDATE point_rules
                SET trigger_action = ?, points = ?, chance = ?, enabled = ?
                WHERE rule_id = ? AND group_id = ?
                """,
                (
                    payload.trigger_action,
                    payload.points,
                    payload.chance,
                    int(payload.enabled),
                    payload.rule_id,
                    group_id,
                ),
            )
            rid = payload.rule_id
        else:
            rid = await db.execute(
                """
                INSERT INTO point_rules (group_id, trigger_action, points, chance, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    payload.trigger_action,
                    payload.points,
                    payload.chance,
                    int(payload.enabled),
                ),
            )
        row = await db.fetchrow(
            "SELECT * FROM point_rules WHERE rule_id = ?", (rid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_point_rule",
        f"{'更新' if payload.rule_id else '新增'}积分规则: {payload.trigger_action} +{payload.points}",
    )
    return {"status": "ok", "rule": row}


@api_router.delete("/api/group/{group_id}/point-rules/{rule_id}")
async def api_delete_point_rule(
    group_id: int,
    rule_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM point_rules WHERE rule_id = ? AND group_id = ?",
            (rule_id, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_point_rule",
        f"删除积分规则 ID={rule_id}",
    )
    return {"status": "ok", "deleted_id": rule_id}


@api_router.get("/api/group/{group_id}/point-settings")
async def api_get_point_settings(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM point_settings WHERE group_id = ?", (group_id,)
        )
    return {"point_settings": row or _default_point_settings(group_id)}


@api_router.post("/api/group/{group_id}/point-settings")
async def api_save_point_settings(
    group_id: int,
    payload: PointSettingsPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    alias = (payload.alias or "积分").strip() or "积分"
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO point_settings
                (group_id, alias, daily_max, decimals, auto_clear_inactive,
                 signin_points, streak_bonus_points, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                alias = excluded.alias,
                daily_max = excluded.daily_max,
                decimals = excluded.decimals,
                auto_clear_inactive = excluded.auto_clear_inactive,
                signin_points = excluded.signin_points,
                streak_bonus_points = excluded.streak_bonus_points,
                enabled = excluded.enabled
            """,
            (
                group_id,
                alias,
                payload.daily_max,
                int(payload.decimals),
                int(payload.auto_clear_inactive),
                payload.signin_points,
                payload.streak_bonus_points,
                int(payload.enabled),
            ),
        )
        row = await db.fetchrow(
            "SELECT * FROM point_settings WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_point_settings",
        f"更新积分设置: 别名={alias}, 每日上限={payload.daily_max}, 签到奖励={payload.signin_points}",
    )
    return {"status": "ok", "point_settings": row}


@api_router.get("/api/group/{group_id}/point-redemptions")
async def api_get_point_redemptions(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM point_redemptions WHERE group_id = ? ORDER BY id DESC",
            (group_id,),
        )
    return {"redemptions": rows}


@api_router.post("/api/group/{group_id}/point-redemptions")
async def api_save_point_redemption(
    group_id: int,
    payload: PointRedemptionPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="兑换关键词不能为空")
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE point_redemptions
                SET keyword = ?, cost = ?, description = ?, enabled = ?
                WHERE id = ? AND group_id = ?
                """,
                (
                    keyword,
                    payload.cost,
                    payload.description.strip(),
                    int(payload.enabled),
                    payload.id,
                    group_id,
                ),
            )
            rid = payload.id
        else:
            rid = await db.execute(
                """
                INSERT INTO point_redemptions (group_id, keyword, cost, description, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    keyword,
                    payload.cost,
                    payload.description.strip(),
                    int(payload.enabled),
                ),
            )
        row = await db.fetchrow(
            "SELECT * FROM point_redemptions WHERE id = ?", (rid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_point_redemption",
        f"{'更新' if payload.id else '新增'}积分兑换: {keyword} (消耗 {payload.cost})",
    )
    return {"status": "ok", "redemption": row}


@api_router.delete("/api/group/{group_id}/point-redemptions/{rid}")
async def api_delete_point_redemption(
    group_id: int,
    rid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM point_redemptions WHERE id = ? AND group_id = ?",
            (rid, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_point_redemption",
        f"删除积分兑换 ID={rid}",
    )
    return {"status": "ok", "deleted_id": rid}


# ==================== Batch 2: 16. 抽奖与游戏 ====================


def _lottery_with_prizes(row: dict, prizes: list) -> dict:
    out = dict(row)
    out["prizes"] = prizes
    return out


@api_router.get("/api/group/{group_id}/lotteries")
async def api_get_lotteries(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM lotteries WHERE group_id = ? ORDER BY id DESC",
            (group_id,),
        )
        result = []
        for row in rows:
            prizes = await db.fetch(
                "SELECT * FROM lottery_prizes WHERE lottery_id = ? ORDER BY id",
                (row["id"],),
            )
            result.append(_lottery_with_prizes(row, prizes))
    return {"lotteries": result}


@api_router.post("/api/group/{group_id}/lotteries")
async def api_save_lottery(
    group_id: int,
    payload: LotteryPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="抽奖标题不能为空")
    keyword = (payload.keyword or "抽奖").strip() or "抽奖"
    draw_keyword = (payload.draw_keyword or "开奖").strip() or "开奖"
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE lotteries
                SET lottery_type = ?, title = ?, keyword = ?, draw_keyword = ?,
                    start_at = ?, end_at = ?, threshold = ?, draw_mode = ?,
                    cost_per_draw = ?, max_draws_per_user = ?, status = ?
                WHERE id = ? AND group_id = ?
                """,
                (
                    payload.lottery_type,
                    title,
                    keyword,
                    draw_keyword,
                    payload.start_at or "",
                    payload.end_at or "",
                    payload.threshold,
                    payload.draw_mode,
                    payload.cost_per_draw,
                    payload.max_draws_per_user,
                    payload.status,
                    payload.id,
                    group_id,
                ),
            )
            lid = payload.id
        else:
            lid = await db.execute(
                """
                INSERT INTO lotteries
                    (group_id, lottery_type, title, keyword, draw_keyword,
                     start_at, end_at, threshold, draw_mode,
                     cost_per_draw, max_draws_per_user, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    payload.lottery_type,
                    title,
                    keyword,
                    draw_keyword,
                    payload.start_at or "",
                    payload.end_at or "",
                    payload.threshold,
                    payload.draw_mode,
                    payload.cost_per_draw,
                    payload.max_draws_per_user,
                    payload.status,
                ),
            )
        if payload.prizes is not None:
            await db.execute(
                "DELETE FROM lottery_prizes WHERE lottery_id = ?", (lid,)
            )
            for prize in payload.prizes:
                name = prize.prize_name.strip()
                if name:
                    await db.execute(
                        """
                        INSERT INTO lottery_prizes (lottery_id, prize_name, total)
                        VALUES (?, ?, ?)
                        """,
                        (lid, name, max(1, prize.total)),
                    )
        row = await db.fetchrow("SELECT * FROM lotteries WHERE id = ?", (lid,))
        prizes = await db.fetch(
            "SELECT * FROM lottery_prizes WHERE lottery_id = ? ORDER BY id", (lid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_lottery",
        f"{'更新' if payload.id else '新增'}抽奖活动: {title} ({payload.lottery_type})",
    )
    return {"status": "ok", "lottery": _lottery_with_prizes(row, prizes)}


@api_router.delete("/api/group/{group_id}/lotteries/{lid}")
async def api_delete_lottery(
    group_id: int,
    lid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM lotteries WHERE id = ? AND group_id = ?", (lid, group_id)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_lottery",
        f"删除抽奖活动 ID={lid}",
    )
    return {"status": "ok", "deleted_id": lid}


@api_router.post("/api/group/{group_id}/lotteries/{lid}/prizes")
async def api_add_lottery_prize(
    group_id: int,
    lid: int,
    payload: LotteryPrizeItem,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    name = payload.prize_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="奖品名称不能为空")
    async with get_db() as db:
        lottery = await db.fetchrow(
            "SELECT id FROM lotteries WHERE id = ? AND group_id = ?", (lid, group_id)
        )
        if not lottery:
            raise HTTPException(status_code=404, detail="抽奖活动不存在")
        pid = await db.execute(
            "INSERT INTO lottery_prizes (lottery_id, prize_name, total) VALUES (?, ?, ?)",
            (lid, name, max(1, payload.total)),
        )
        row = await db.fetchrow(
            "SELECT * FROM lottery_prizes WHERE id = ?", (pid,)
        )
    return {"status": "ok", "prize": row}


@api_router.delete("/api/group/{group_id}/lotteries/{lid}/prizes/{pid}")
async def api_delete_lottery_prize(
    group_id: int,
    lid: int,
    pid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM lottery_prizes WHERE id = ? AND lottery_id = ?",
            (pid, lid),
        )
    return {"status": "ok", "deleted_id": pid}


@api_router.post("/api/group/{group_id}/lotteries/{lid}/draw")
async def api_draw_lottery(
    group_id: int,
    lid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot 未在线，无法执行开奖")
    async with get_db() as db:
        lottery = await db.fetchrow(
            "SELECT * FROM lotteries WHERE id = ? AND group_id = ?", (lid, group_id)
        )
    if not lottery:
        raise HTTPException(status_code=404, detail="抽奖活动不存在")
    if lottery.get("status") == "ended":
        raise HTTPException(status_code=400, detail="该抽奖已结束")
    from app.bot.handlers.games import _draw_lottery

    notice = await _draw_lottery(bot, lottery)
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "draw_lottery",
        f"手动开奖: {lottery.get('title') or lid}",
    )
    return {"status": "ok", "notice": notice}


@api_router.get("/api/group/{group_id}/lotteries/{lid}/entries")
async def api_get_lottery_entries(
    group_id: int,
    lid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        entries = await db.fetch(
            "SELECT * FROM lottery_entries WHERE lottery_id = ? ORDER BY id DESC LIMIT 100",
            (lid,),
        )
        draws = await db.fetch(
            "SELECT * FROM lottery_draws WHERE lottery_id = ? ORDER BY id DESC LIMIT 100",
            (lid,),
        )
    return {"entries": entries, "draws": draws}


@api_router.get("/api/group/{group_id}/guesses")
async def api_get_guesses(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM guesses WHERE group_id = ? ORDER BY id DESC", (group_id,)
        )
        result = []
        for row in rows:
            options = await db.fetch(
                "SELECT * FROM guess_options WHERE guess_id = ? ORDER BY id",
                (row["id"],),
            )
            bet_count = await db.fetchrow(
                "SELECT COUNT(*) AS cnt FROM guess_bets WHERE guess_id = ?",
                (row["id"],),
            )
            out = dict(row)
            out["options"] = options
            out["bet_count"] = bet_count["cnt"] if bet_count else 0
            result.append(out)
    return {"guesses": result}


@api_router.post("/api/group/{group_id}/guesses")
async def api_save_guess(
    group_id: int,
    payload: GuessPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    title = payload.title.strip()
    question = payload.question.strip()
    if not title or not question:
        raise HTTPException(status_code=400, detail="竞猜标题与问题均不能为空")
    if payload.id is None and not payload.options:
        raise HTTPException(status_code=400, detail="新建竞猜至少需要一个选项")
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE guesses
                SET title = ?, question = ?, cost_per_bet = ?, odds_mode = ?,
                    prize_pool = ?, prize_amount = ?
                WHERE id = ? AND group_id = ?
                """,
                (
                    title,
                    question,
                    payload.cost_per_bet,
                    payload.odds_mode,
                    payload.prize_pool,
                    payload.prize_amount,
                    payload.id,
                    group_id,
                ),
            )
            gid = payload.id
        else:
            gid = await db.execute(
                """
                INSERT INTO guesses
                    (group_id, title, question, cost_per_bet, odds_mode, prize_pool, prize_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    title,
                    question,
                    payload.cost_per_bet,
                    payload.odds_mode,
                    payload.prize_pool,
                    payload.prize_amount,
                ),
            )
        if payload.options is not None:
            await db.execute(
                "DELETE FROM guess_options WHERE guess_id = ?", (gid,)
            )
            for opt in payload.options:
                text = opt.option_text.strip()
                if text:
                    await db.execute(
                        "INSERT INTO guess_options (guess_id, option_text, odds) VALUES (?, ?, ?)",
                        (gid, text, opt.odds),
                    )
        row = await db.fetchrow("SELECT * FROM guesses WHERE id = ?", (gid,))
        options = await db.fetch(
            "SELECT * FROM guess_options WHERE guess_id = ? ORDER BY id", (gid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_guess",
        f"{'更新' if payload.id else '新增'}积分竞猜: {title}",
    )
    out = dict(row)
    out["options"] = options
    return {"status": "ok", "guess": out}


@api_router.post("/api/group/{group_id}/guesses/{gid}/post")
async def api_post_guess(
    group_id: int,
    gid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot 未在线，无法发布竞猜")
    async with get_db() as db:
        guess = await db.fetchrow(
            "SELECT * FROM guesses WHERE id = ? AND group_id = ?", (gid, group_id)
        )
    if not guess:
        raise HTTPException(status_code=404, detail="竞猜不存在")
    if guess.get("status") != "active":
        raise HTTPException(status_code=400, detail="该竞猜已结束，无法发布")
    from app.bot.handlers.games import post_guess_message

    message_id = await post_guess_message(bot, group_id, guess)
    if message_id is None:
        raise HTTPException(status_code=502, detail="发布竞猜失败（请先添加选项）")
    return {"status": "ok", "posted_message_id": message_id}


@api_router.post("/api/group/{group_id}/guesses/{gid}/close")
async def api_close_guess(
    group_id: int,
    gid: int,
    payload: GuessClosePayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot 未在线，无法结算竞猜")
    async with get_db() as db:
        guess = await db.fetchrow(
            "SELECT * FROM guesses WHERE id = ? AND group_id = ?", (gid, group_id)
        )
    if not guess:
        raise HTTPException(status_code=404, detail="竞猜不存在")
    from app.bot.handlers.games import close_guess_and_payout

    notice = await close_guess_and_payout(bot, gid, payload.winning_option_id)
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "close_guess",
        f"结算竞猜 ID={gid}，中奖选项 ID={payload.winning_option_id}",
    )
    return {"status": "ok", "notice": notice}


@api_router.delete("/api/group/{group_id}/guesses/{gid}")
async def api_delete_guess(
    group_id: int,
    gid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM guesses WHERE id = ? AND group_id = ?", (gid, group_id)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_guess",
        f"删除积分竞猜 ID={gid}",
    )
    return {"status": "ok", "deleted_id": gid}


@api_router.get("/api/group/{group_id}/guesses/{gid}/bets")
async def api_get_guess_bets(
    group_id: int,
    gid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM guess_bets WHERE guess_id = ? ORDER BY id DESC LIMIT 100",
            (gid,),
        )
    return {"bets": rows}


@api_router.get("/api/group/{group_id}/dice-settings")
async def api_get_dice_settings(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM dice_settings WHERE group_id = ?", (group_id,)
        )
    if not row:
        row = {
            "group_id": group_id,
            "enabled": 0,
            "cost_per_round": 10,
            "payout_multiplier": 2.0,
            "house_cut_percent": 0,
        }
    return {"dice_settings": row}


@api_router.post("/api/group/{group_id}/dice-settings")
async def api_save_dice_settings(
    group_id: int,
    payload: DiceSettingsPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO dice_settings
                (group_id, enabled, cost_per_round, payout_multiplier, house_cut_percent)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                enabled = excluded.enabled,
                cost_per_round = excluded.cost_per_round,
                payout_multiplier = excluded.payout_multiplier,
                house_cut_percent = excluded.house_cut_percent
            """,
            (
                group_id,
                int(payload.enabled),
                payload.cost_per_round,
                payload.payout_multiplier,
                payload.house_cut_percent,
            ),
        )
        row = await db.fetchrow(
            "SELECT * FROM dice_settings WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_dice_settings",
        f"更新骰子游戏: 开启={payload.enabled}, 每局消耗={payload.cost_per_round}, 赔付倍数={payload.payout_multiplier}",
    )
    return {"status": "ok", "dice_settings": row}


@api_router.get("/api/group/{group_id}/dice-records")
async def api_get_dice_records(
    group_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    page: int = 1,
    page_size: int = 20,
) -> dict:
    await require_group_admin(request, group_id, user)
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    async with get_db() as db:
        total_row = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM dice_records WHERE group_id = ?", (group_id,)
        )
        rows = await db.fetch(
            """
            SELECT * FROM dice_records WHERE group_id = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (group_id, page_size, (page - 1) * page_size),
        )
    return {
        "records": rows,
        "total": total_row["cnt"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


# ==================== Batch 2: 17. 轮播广告 ====================


@api_router.get("/api/group/{group_id}/rotating-ads")
async def api_get_rotating_ads(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        ads = await db.fetch(
            "SELECT * FROM rotating_ads WHERE group_id = ? ORDER BY id",
            (group_id,),
        )
        cfg = await db.fetchrow(
            "SELECT * FROM rotating_ads_settings WHERE group_id = ?", (group_id,)
        )
    if not cfg:
        cfg = {
            "group_id": group_id,
            "enabled": 0,
            "interval_minutes": 60,
            "display_seconds": 0,
        }
    return {"ads": ads, "settings": cfg}


@api_router.post("/api/group/{group_id}/rotating-ads")
async def api_save_rotating_ad(
    group_id: int,
    payload: RotatingAdPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    if not (payload.image_url or "").strip() and not (payload.text or "").strip():
        raise HTTPException(status_code=400, detail="广告至少需要图片或文案")
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE rotating_ads
                SET image_url = ?, link_url = ?, text = ?, enabled = ?
                WHERE id = ? AND group_id = ?
                """,
                (
                    payload.image_url.strip(),
                    payload.link_url.strip(),
                    payload.text.strip(),
                    int(payload.enabled),
                    payload.id,
                    group_id,
                ),
            )
            aid = payload.id
        else:
            aid = await db.execute(
                """
                INSERT INTO rotating_ads (group_id, image_url, link_url, text, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    payload.image_url.strip(),
                    payload.link_url.strip(),
                    payload.text.strip(),
                    int(payload.enabled),
                ),
            )
        row = await db.fetchrow(
            "SELECT * FROM rotating_ads WHERE id = ?", (aid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_rotating_ad",
        f"{'更新' if payload.id else '新增'}轮播广告 ID={aid}",
    )
    return {"status": "ok", "ad": row}


@api_router.delete("/api/group/{group_id}/rotating-ads/{aid}")
async def api_delete_rotating_ad(
    group_id: int,
    aid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM rotating_ads WHERE id = ? AND group_id = ?", (aid, group_id)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_rotating_ad",
        f"删除轮播广告 ID={aid}",
    )
    return {"status": "ok", "deleted_id": aid}


@api_router.post("/api/group/{group_id}/rotating-ads-settings")
async def api_save_rotating_ads_settings(
    group_id: int,
    payload: RotatingAdsSettingsPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO rotating_ads_settings (group_id, enabled, interval_minutes, display_seconds)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                enabled = excluded.enabled,
                interval_minutes = excluded.interval_minutes,
                display_seconds = excluded.display_seconds
            """,
            (
                group_id,
                int(payload.enabled),
                payload.interval_minutes,
                payload.display_seconds,
            ),
        )
        row = await db.fetchrow(
            "SELECT * FROM rotating_ads_settings WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_rotating_ads_settings",
        f"更新轮播广告设置: 开启={payload.enabled}, 间隔={payload.interval_minutes}分钟",
    )
    return {"status": "ok", "settings": row}


# ==================== Batch 2: 18. 邀请活动 ====================


@api_router.get("/api/group/{group_id}/invites")
async def api_get_invites(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        leaderboard = await db.fetch(
            """
            SELECT inviter_id, COUNT(*) AS cnt FROM invites
            WHERE group_id = ? AND inviter_id IS NOT NULL
            GROUP BY inviter_id ORDER BY cnt DESC LIMIT 50
            """,
            (group_id,),
        )
        total = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM invites WHERE group_id = ?", (group_id,)
        )
        recent = await db.fetch(
            """
            SELECT * FROM invites WHERE group_id = ?
            ORDER BY id DESC LIMIT 50
            """,
            (group_id,),
        )
    return {
        "leaderboard": leaderboard,
        "total": total["cnt"] if total else 0,
        "recent": recent,
    }


@api_router.post("/api/group/{group_id}/invites/clear")
async def api_clear_invites(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM invites WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "clear_invites",
        "清空邀请活动数据",
    )
    return {"status": "ok", "cleared": True}


# ==================== Batch 2: 19. 定时关群 / 话术关群 ====================


@api_router.get("/api/group/{group_id}/scheduled-lockdowns")
async def api_get_scheduled_lockdowns(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM scheduled_lockdowns WHERE group_id = ? ORDER BY start_time",
            (group_id,),
        )
    return {"lockdowns": rows}


@api_router.post("/api/group/{group_id}/scheduled-lockdowns")
async def api_save_scheduled_lockdown(
    group_id: int,
    payload: ScheduledLockdownPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    for t in (payload.start_time, payload.end_time):
        hh, mm = t.split(":")
        if int(hh) > 23 or int(mm) > 59:
            raise HTTPException(status_code=400, detail="时间格式无效 (应为 HH:MM)")
    if payload.start_time == payload.end_time:
        raise HTTPException(
            status_code=400, detail="开始时间不能等于结束时间 (否则该规则永远不触发)"
        )
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE scheduled_lockdowns
                SET start_time = ?, end_time = ?, enabled = ?
                WHERE id = ? AND group_id = ?
                """,
                (
                    payload.start_time,
                    payload.end_time,
                    int(payload.enabled),
                    payload.id,
                    group_id,
                ),
            )
            lid = payload.id
        else:
            lid = await db.execute(
                """
                INSERT INTO scheduled_lockdowns (group_id, start_time, end_time, enabled)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, payload.start_time, payload.end_time, int(payload.enabled)),
            )
        row = await db.fetchrow(
            "SELECT * FROM scheduled_lockdowns WHERE id = ?", (lid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_scheduled_lockdown",
        f"{'更新' if payload.id else '新增'}定时关群: {payload.start_time}-{payload.end_time}",
    )
    return {"status": "ok", "lockdown": row}


@api_router.delete("/api/group/{group_id}/scheduled-lockdowns/{lid}")
async def api_delete_scheduled_lockdown(
    group_id: int,
    lid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM scheduled_lockdowns WHERE id = ? AND group_id = ?",
            (lid, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_scheduled_lockdown",
        f"删除定时关群 ID={lid}",
    )
    return {"status": "ok", "deleted_id": lid}


@api_router.get("/api/group/{group_id}/lockdown-keywords")
async def api_get_lockdown_keywords(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM lockdown_keywords WHERE group_id = ? ORDER BY id DESC",
            (group_id,),
        )
    return {"keywords": rows}


@api_router.post("/api/group/{group_id}/lockdown-keywords")
async def api_save_lockdown_keyword(
    group_id: int,
    payload: LockdownKeywordPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关群关键词不能为空")
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE lockdown_keywords
                SET keyword = ?, lock_minutes = ?, enabled = ?, allow_admin_trigger = ?
                WHERE id = ? AND group_id = ?
                """,
                (
                    keyword,
                    payload.lock_minutes,
                    int(payload.enabled),
                    int(payload.allow_admin_trigger),
                    payload.id,
                    group_id,
                ),
            )
            kid = payload.id
        else:
            kid = await db.execute(
                """
                INSERT INTO lockdown_keywords (group_id, keyword, lock_minutes, enabled, allow_admin_trigger)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, keyword, payload.lock_minutes, int(payload.enabled), int(payload.allow_admin_trigger)),
            )
        row = await db.fetchrow(
            "SELECT * FROM lockdown_keywords WHERE id = ?", (kid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_lockdown_keyword",
        f"{'更新' if payload.id else '新增'}话术关群关键词: {keyword} (锁 {payload.lock_minutes} 分钟)",
    )
    return {"status": "ok", "keyword": row}


@api_router.delete("/api/group/{group_id}/lockdown-keywords/{kid}")
async def api_delete_lockdown_keyword(
    group_id: int,
    kid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM lockdown_keywords WHERE id = ? AND group_id = ?",
            (kid, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_lockdown_keyword",
        f"删除话术关群关键词 ID={kid}",
    )
    return {"status": "ok", "deleted_id": kid}


# ==================== Batch 2: 20. 退群拉黑 / 改名监控 / 底部按钮 / 其他设置 ====================


def _default_toggle_row(group_id: int) -> dict:
    return {"group_id": group_id, "enabled": 0}


@api_router.get("/api/group/{group_id}/leave-ban")
async def api_get_leave_ban(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_leave_ban WHERE group_id = ?", (group_id,)
        )
    return {"leave_ban": row or _default_toggle_row(group_id)}


@api_router.post("/api/group/{group_id}/leave-ban")
async def api_save_leave_ban(
    group_id: int,
    payload: TogglePayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_leave_ban (group_id, enabled) VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET enabled = excluded.enabled
            """,
            (group_id, int(payload.enabled)),
        )
        row = await db.fetchrow(
            "SELECT * FROM group_leave_ban WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_leave_ban",
        f"更新退群拉黑: 开启={payload.enabled}",
    )
    return {"status": "ok", "leave_ban": row}


@api_router.get("/api/group/{group_id}/rename-watch")
async def api_get_rename_watch(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_rename_watch WHERE group_id = ?", (group_id,)
        )
    return {"rename_watch": row or _default_toggle_row(group_id)}


@api_router.post("/api/group/{group_id}/rename-watch")
async def api_save_rename_watch(
    group_id: int,
    payload: TogglePayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_rename_watch (group_id, enabled) VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET enabled = excluded.enabled
            """,
            (group_id, int(payload.enabled)),
        )
        row = await db.fetchrow(
            "SELECT * FROM group_rename_watch WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_rename_watch",
        f"更新改名监控: 开启={payload.enabled}",
    )
    return {"status": "ok", "rename_watch": row}


@api_router.get("/api/group/{group_id}/bottom-buttons")
async def api_get_bottom_buttons(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM bottom_buttons WHERE group_id = ? ORDER BY id",
            (group_id,),
        )
    return {"buttons": rows}


@api_router.post("/api/group/{group_id}/bottom-buttons")
async def api_save_bottom_button(
    group_id: int,
    payload: BottomButtonPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    text = payload.button_text.strip()
    reply = payload.reply_text.strip()
    if not text or not reply:
        raise HTTPException(status_code=400, detail="按钮文字与回复内容均不能为空")
    async with get_db() as db:
        if payload.id:
            await db.execute(
                """
                UPDATE bottom_buttons
                SET button_text = ?, reply_text = ?, enabled = ?
                WHERE id = ? AND group_id = ?
                """,
                (text, reply, int(payload.enabled), payload.id, group_id),
            )
            bid = payload.id
        else:
            bid = await db.execute(
                """
                INSERT INTO bottom_buttons (group_id, button_text, reply_text, enabled)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, text, reply, int(payload.enabled)),
            )
        row = await db.fetchrow(
            "SELECT * FROM bottom_buttons WHERE id = ?", (bid,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "save_bottom_button",
        f"{'更新' if payload.id else '新增'}底部按钮: {text}",
    )
    return {"status": "ok", "button": row}


@api_router.delete("/api/group/{group_id}/bottom-buttons/{bid}")
async def api_delete_bottom_button(
    group_id: int,
    bid: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        await db.execute(
            "DELETE FROM bottom_buttons WHERE id = ? AND group_id = ?",
            (bid, group_id),
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "delete_bottom_button",
        f"删除底部按钮 ID={bid}",
    )
    return {"status": "ok", "deleted_id": bid}


@api_router.get("/api/group/{group_id}/misc-settings")
async def api_get_misc_settings(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_misc_settings WHERE group_id = ?", (group_id,)
        )
    if not row:
        row = {
            "group_id": group_id,
            "auto_delete_join": 1,
            "auto_delete_leave": 1,
            "auto_delete_pinned_notice": 1,
            "auto_delete_promotion": 0,
            "cancel_channel_pin": 0,
            "calculator": 0,
            "auto_check_u": 0,
        }
    return {"misc_settings": row}


@api_router.post("/api/group/{group_id}/misc-settings")
async def api_save_misc_settings(
    group_id: int,
    payload: MiscSettingsPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await require_group_admin(request, group_id, user)
    await ensure_group_initialized(group_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO group_misc_settings
                (group_id, auto_delete_join, auto_delete_leave, auto_delete_pinned_notice,
                 auto_delete_promotion, cancel_channel_pin, calculator, auto_check_u)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                auto_delete_join = excluded.auto_delete_join,
                auto_delete_leave = excluded.auto_delete_leave,
                auto_delete_pinned_notice = excluded.auto_delete_pinned_notice,
                auto_delete_promotion = excluded.auto_delete_promotion,
                cancel_channel_pin = excluded.cancel_channel_pin,
                calculator = excluded.calculator,
                auto_check_u = excluded.auto_check_u
            """,
            (
                group_id,
                int(payload.auto_delete_join),
                int(payload.auto_delete_leave),
                int(payload.auto_delete_pinned_notice),
                int(payload.auto_delete_promotion),
                int(payload.cancel_channel_pin),
                int(payload.calculator),
                int(payload.auto_check_u),
            ),
        )
        row = await db.fetchrow(
            "SELECT * FROM group_misc_settings WHERE group_id = ?", (group_id,)
        )
    await record_audit_log(
        group_id,
        int(user["id"]),
        user.get("first_name") or "Admin",
        "update_misc_settings",
        "更新其他设置 (自动删除服务消息 / 计算器 / 自动查U)",
    )
    return {"status": "ok", "misc_settings": row}


# ==================== Batch 2: 21. 数据看板 (今日计数) ====================


@api_router.get("/api/group/{group_id}/dashboard")
async def api_get_dashboard(
    group_id: int, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    await require_group_admin(request, group_id, user)
    async with get_db() as db:
        today = await db.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM group_messages
            WHERE group_id = ? AND date(created_at, '+8 hours') = date('now', '+8 hours')
            """,
            (group_id,),
        )
        joins = await db.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM group_members
            WHERE group_id = ? AND date(joined_at, '+8 hours') = date('now', '+8 hours')
            """,
            (group_id,),
        )
        leaves = await db.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM admin_audit_logs
            WHERE group_id = ? AND action_type = 'member_leave'
              AND date(created_at, '+8 hours') = date('now', '+8 hours')
            """,
            (group_id,),
        )
        kicks = await db.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM admin_audit_logs
            WHERE group_id = ? AND action_type LIKE 'member_%'
              AND date(created_at, '+8 hours') = date('now', '+8 hours')
            """,
            (group_id,),
        )
        signins = await db.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM point_logs
            WHERE group_id = ? AND reason LIKE '签到%'
              AND date(created_at, '+8 hours') = date('now', '+8 hours')
            """,
            (group_id,),
        )
        verifications = await db.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM admin_audit_logs
            WHERE group_id = ? AND action_type LIKE 'captcha%'
              AND date(created_at, '+8 hours') = date('now', '+8 hours')
            """,
            (group_id,),
        )
    return {
        "messages_today": today["cnt"] if today else 0,
        "joins_today": joins["cnt"] if joins else 0,
        "leaves_today": leaves["cnt"] if leaves else 0,
        "kicks_today": kicks["cnt"] if kicks else 0,
        "signins_today": signins["cnt"] if signins else 0,
        "verifications_today": verifications["cnt"] if verifications else 0,
    }
