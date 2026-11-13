# -*- coding: utf-8 -*-
"""永久禁言测试（SQLite 本地）：阈值动作与单条规则动作新增 mute_permanent。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    ~/workspace/.venv-bot-test/bin/python tests/test_permanent_mute_20261113.py

验证点：
  1. 达到阈值且 warn_action=mute_permanent 时，restrict 不带 until_date（永久禁言）
  2. 单条规则 punish_action=mute_permanent 时同样不带 until_date
  3. 回归：普通 mute 仍带 until_date = 消息时间 + mute_duration
  4. API 校验：ModerationConfigPayload / SensitiveRuleItem 接受 mute_permanent，拒绝非法值
  5. 前端：两个下拉框 + 规则徽章都有永久禁言选项
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ["DATABASE_PATH"] = "/tmp/test_permanent_mute.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.types import Chat, Message, User
from pydantic import ValidationError

from app.api.routes import ModerationConfigPayload, SensitiveRuleItem
from app.bot.handlers.moderation import inspect_and_moderate_message
from app.database.db import close_db, get_db, init_db

GROUP = -1004496842671
MEMBER = 111222333


class FakeBot:
    """只记录调用，不发真请求。"""

    def __init__(self):
        self.restricts = []
        self.calls = []

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.calls.append(("delete_message", chat_id, message_id))
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send_message", chat_id, text))
        return None

    async def restrict_chat_member(self, chat_id, user_id, permissions, **kwargs):
        self.restricts.append(
            {"chat_id": chat_id, "user_id": user_id,
             "permissions": permissions, **kwargs}
        )
        return True

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("ban_chat_member", chat_id, user_id))
        return True

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("unban_chat_member", chat_id, user_id))
        return True


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def violation_message(text="加微领币"):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP, type="supergroup", title="拥抱A i"),
        from_user=User(id=MEMBER, is_bot=False, first_name="小明", username="xiaoming"),
        text=text,
    )


async def fresh_db():
    await close_db()
    if os.path.exists("/tmp/test_permanent_mute.db"):
        os.remove("/tmp/test_permanent_mute.db")
    await init_db()
    # 审核/规则/累计/审计表都有 group_id → group_settings 的外键，先种父行
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_settings (group_id, group_title) VALUES (?, '拥抱A i')",
            (GROUP,),
        )


async def seed_settings(warn_action, max_warn_count):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO group_moderation_settings
               (group_id, send_warning_msg, warning_ttl, max_warn_count, warn_action)
               VALUES (?, 0, 0, ?, ?)""",
            (GROUP, max_warn_count, warn_action),
        )


async def seed_rule(punish, mute_duration=3600):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO group_sensitive_rules
               (group_id, pattern, match_type, punish_action, mute_duration)
               VALUES (?, '加微', 'contains', ?, ?)""",
            (GROUP, punish, mute_duration),
        )


async def test_threshold_mute_permanent():
    """阈值动作 mute_permanent：restrict 不带 until_date。"""
    await fresh_db()
    await seed_settings("mute_permanent", 1)
    await seed_rule("delete_only")
    bot = FakeBot()
    msg = violation_message()
    ok = True
    ok &= check("违规被拦截", await inspect_and_moderate_message(msg, bot) is True)
    ok &= check("调了一次 restrict", len(bot.restricts) == 1)
    call = bot.restricts[0] if bot.restricts else {}
    ok &= check("禁言目标是违规用户", call.get("user_id") == MEMBER)
    ok &= check("不能发消息", call.get("permissions") is not None
                and call["permissions"].can_send_messages is False)
    ok &= check("没传 until_date（永久）", "until_date" not in call)
    return ok


async def test_rule_mute_permanent():
    """单条规则动作 mute_permanent：未达阈值时同样永久禁言。"""
    await fresh_db()
    await seed_settings("kick", 3)
    await seed_rule("mute_permanent")
    bot = FakeBot()
    ok = True
    ok &= check("违规被拦截", await inspect_and_moderate_message(violation_message(), bot) is True)
    ok &= check("调了一次 restrict", len(bot.restricts) == 1)
    call = bot.restricts[0] if bot.restricts else {}
    ok &= check("没传 until_date（永久）", "until_date" not in call)
    return ok


async def test_mute_still_has_until_date():
    """回归：普通 mute 仍带 until_date = 消息时间 + 时长。"""
    await fresh_db()
    await seed_settings("kick", 3)
    await seed_rule("mute", 3600)
    bot = FakeBot()
    msg = violation_message()
    ok = True
    ok &= check("违规被拦截", await inspect_and_moderate_message(msg, bot) is True)
    ok &= check("调了一次 restrict", len(bot.restricts) == 1)
    call = bot.restricts[0] if bot.restricts else {}
    expected = int(msg.date.timestamp()) + 3600
    ok &= check("带 until_date", call.get("until_date") == expected)
    return ok


async def test_payload_validation():
    ok = True
    try:
        ModerationConfigPayload(warn_action="mute_permanent")
        ok &= check("阈值动作接受 mute_permanent", True)
    except ValidationError:
        ok &= check("阈值动作接受 mute_permanent", False)
    try:
        ModerationConfigPayload(warn_action="mute_forever")
        ok &= check("阈值动作拒绝非法值", False)
    except ValidationError:
        ok &= check("阈值动作拒绝非法值", True)
    try:
        SensitiveRuleItem(pattern="x", punish_action="mute_permanent")
        ok &= check("单条规则接受 mute_permanent", True)
    except ValidationError:
        ok &= check("单条规则接受 mute_permanent", False)
    return ok


async def test_frontend_options():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "app/web/templates/index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    ok = True
    ok &= check("两个下拉框都有 mute_permanent",
                html.count('value="mute_permanent"') >= 2)
    ok &= check("规则徽章有永久禁言", "mute_permanent:" in html
                and "永久禁言" in html)
    return ok


async def main():
    results = []
    results.append(await test_threshold_mute_permanent())
    results.append(await test_rule_mute_permanent())
    results.append(await test_mute_still_has_until_date())
    results.append(await test_payload_validation())
    results.append(await test_frontend_options())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
