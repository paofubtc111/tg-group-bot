import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

# 管理员权限短期内存缓存: (group_id, user_id) -> (is_admin, expire_ts)
_admin_cache: Dict[Tuple[int, int], Tuple[bool, float]] = {}
ADMIN_CACHE_TTL = 60.0


def verify_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """
    校验 Telegram Mini App 发送的 initData 签名真实性并返回用户信息
    """
    try:
        if not init_data or not bot_token:
            return None
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            return None

        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )

        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if hmac.compare_digest(calculated_hash, received_hash):
            user_raw = parsed_data.get("user", "{}")
            return json.loads(user_raw)
        return None
    except Exception as exc:
        logger.warning("校验 Telegram initData 失败: %s", exc)
        return None


async def get_current_user(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> dict:
    """
    FastAPI 依赖项：从 Header 提取并校验 X-Telegram-Init-Data
    """
    if x_telegram_init_data:
        user = verify_init_data(x_telegram_init_data, settings.BOT_TOKEN)
        if user and "id" in user:
            return user

    if settings.DEV_MODE:
        return {
            "id": settings.DEV_ADMIN_ID,
            "first_name": "DevAdmin",
            "username": "dev_admin",
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Telegram WebApp 签名校验失败或缺少 X-Telegram-Init-Data 请求头",
    )


async def check_user_is_group_admin(
    request: Request, group_id: int, user_id: int
) -> bool:
    """
    动态调用 Telegram Bot API get_chat_administrators 校验用户是否具备指定群组的管理员权限
    """
    if settings.DEV_MODE:
        return True

    now = time.time()
    cache_key = (group_id, user_id)
    cached = _admin_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    bot = getattr(request.app.state, "bot", None)
    if not bot:
        return False

    try:
        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {adm.user.id for adm in admins}
        is_adm = user_id in admin_ids
        _admin_cache[cache_key] = (is_adm, now + ADMIN_CACHE_TTL)
        return is_adm
    except Exception as exc:
        logger.warning("获取群组 (%s) 管理员列表失败: %s", group_id, exc)
        return False


async def require_group_admin(
    request: Request, group_id: int, user: dict
) -> None:
    """
    若当前用户不是目标群组的管理员，抛出 403 Forbidden
    """
    is_adm = await check_user_is_group_admin(request, group_id, int(user["id"]))
    if not is_adm:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"越权访问拦截：您不是群组 {group_id} 的管理员",
        )
