import json
import logging
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


class EmergencyConfigPayload(BaseModel):
    lockdown_mode: bool = False
    raid_protection: bool = False
    enable_admin_call: bool = True
    notify_admins_dm: bool = False


class GroupImportPayload(BaseModel):
    captcha: Optional[CaptchaConfigPayload] = None
    welcome: Optional[WelcomeConfigPayload] = None
    moderation: Optional[ModerationConfigPayload] = None
    llm: Optional[LLMConfigPayload] = None
    emergency: Optional[EmergencyConfigPayload] = None


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
                send_warning_msg = ?, warning_ttl = ?, max_warn_count = ?, warn_action = ?
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
                group_id,
            ),
        )

        # 若传入了完整的规则列表，则全量同步更新
        if payload.rules is not None:
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
            "SELECT block_tg_links, block_all_urls, block_forwards, send_warning_msg, warning_ttl, max_warn_count, warn_action FROM group_moderation_settings WHERE group_id = ?",
            (group_id,),
        )
        rules = await db.fetch(
            "SELECT pattern, match_type, punish_action, mute_duration FROM group_sensitive_rules WHERE group_id = ?",
            (group_id,),
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
        "version": "1.0",
        "source_group_id": group_id,
        "captcha": captcha,
        "welcome": welcome,
        "moderation": moderation,
        "llm": llm,
        "emergency": emergency,
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

    if payload.captcha:
        await api_save_captcha(group_id, payload.captcha, request, user)
    if payload.welcome:
        await api_save_welcome(group_id, payload.welcome, request, user)
    if payload.moderation:
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
