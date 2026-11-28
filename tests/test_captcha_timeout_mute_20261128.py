# -*- coding: utf-8 -*-
"""入群验证超时处罚新增永久禁言（v4.2）测试。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \\
    ~/workspace/.venv-bot-test/bin/python tests/test_captcha_timeout_mute_20261128.py

覆盖：
- API 校验：CaptchaConfigPayload 接受 mute_permanent，拒绝 mute/kickk/空串；
- 超时 worker 在 mute_permanent 时调 restrict（RESTRICTED_PERMISSIONS、不带
  until_date）、不调 ban_chat_member，并发出含用户 mention、“永久禁言”、
  “联系管理员”的通知；restrict 抛异常时不发通知、不崩；
- kick/ban 行为不变（kick=ban+unban，ban=只 ban，都不发通知）；
- 验证卡片提示文字三分支：kick→移出群聊、ban→封禁拉黑、
  mute_permanent→永久禁言（可联系管理员解除）；
- 面板：cap-fail-action 下拉框有 mute_permanent 选项，实时预览三分支一致。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

os.environ["DATABASE_PATH"] = "/tmp/test_captcha_timeout_mute.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.types import User
from pydantic import ValidationError

import app.bot.handlers.captcha as captcha
from app.api.routes import CaptchaConfigPayload
from app.bot.handlers.captcha import (
    RESTRICTED_PERMISSIONS,
    _verification_timeout_worker,
    handle_new_member_join,
)
from app.database.db import close_db, get_db, init_db

GROUP = -1004496842671
USER_ID = 777888999


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


class FakeBot:
    """只记录调用，不发真请求。"""

    def __init__(self, fail_restrict=False):
        self.fail_restrict = fail_restrict
        self.restricts = []
        self.bans = []
        self.unbans = []
        self.sent = []
        self.deleted = []
        self._msg_seq = 100

    async def restrict_chat_member(self, chat_id, user_id, permissions, **kwargs):
        if self.fail_restrict:
            raise RuntimeError("no admin rights")
        self.restricts.append(
            {"chat_id": chat_id, "user_id": user_id,
             "permissions": permissions, **kwargs}
        )
        return True

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.bans.append((chat_id, user_id))
        return True

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.unbans.append((chat_id, user_id))
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        self._msg_seq += 1
        return SimpleNamespace(message_id=self._msg_seq)

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.deleted.append((chat_id, message_id))
        return True


async def fresh_db():
    await close_db()
    if os.path.exists("/tmp/test_captcha_timeout_mute.db"):
        os.remove("/tmp/test_captcha_timeout_mute.db")
    await init_db()
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO group_settings (group_id, group_title, is_active)"
            " VALUES (?, ?, 1)",
            (GROUP, "拥抱A i"),
        )


async def seed_captcha(action, timeout=60, auto_delete=True):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO group_captcha_settings"
            " (group_id, enabled, captcha_type, timeout, action_on_fail,"
            "  auto_delete_prompt) VALUES (?, 1, 'math', ?, ?, ?)",
            (GROUP, timeout, action, 1 if auto_delete else 0),
        )
        await db.execute(
            "INSERT OR REPLACE INTO pending_verifications"
            " (group_id, user_id, message_id, expected_answer, expire_at)"
            " VALUES (?, ?, 777, '23', '2099-01-01T00:00:00+00:00')",
            (GROUP, USER_ID),
        )


async def cancel_timeout_tasks():
    for task in list(captcha._active_tasks.values()):
        task.cancel()
    await asyncio.sleep(0)


async def test_payload_validation():
    ok = True
    try:
        CaptchaConfigPayload(action_on_fail="mute_permanent")
        ok &= check("Payload 接受 mute_permanent", True)
    except ValidationError:
        ok &= check("Payload 接受 mute_permanent", False)
    for bad in ("mute", "kickk", ""):
        try:
            CaptchaConfigPayload(action_on_fail=bad)
            ok &= check(f"Payload 拒绝非法值 {bad!r}", False)
        except ValidationError:
            ok &= check(f"Payload 拒绝非法值 {bad!r}", True)
    ok &= check("Payload 仍接受 kick/ban",
                CaptchaConfigPayload(action_on_fail="kick").action_on_fail == "kick"
                and CaptchaConfigPayload(action_on_fail="ban").action_on_fail == "ban")
    return ok


async def test_worker_mute_permanent():
    await fresh_db()
    await seed_captcha("mute_permanent")
    bot = FakeBot()
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    ok = True
    ok &= check("mute_permanent 调一次 restrict",
                len(bot.restricts) == 1)
    r = bot.restricts[0] if bot.restricts else {}
    ok &= check("restrict 用 RESTRICTED_PERMISSIONS 且不带 until_date",
                r.get("permissions") is RESTRICTED_PERMISSIONS
                and "until_date" not in r)
    ok &= check("mute_permanent 不调 ban/unban",
                bot.bans == [] and bot.unbans == [])
    notes = [s for s in bot.sent if "永久禁言" in s["text"]]
    ok &= check("mute_permanent 发出禁言通知", len(notes) == 1)
    note = notes[0]["text"] if notes else ""
    ok &= check("通知含用户 mention 链接",
                f"tg://user?id={USER_ID}" in note)
    ok &= check("通知含“联系管理员”", "联系管理员" in note)
    ok &= check("通知说明仍在群内", "仍留在群内" in note)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT action_type FROM admin_audit_logs"
            " WHERE group_id = ? ORDER BY log_id DESC LIMIT 1",
            (GROUP,),
        )
    ok &= check("审计日志记 captcha_timeout_mute_permanent",
                row is not None
                and row["action_type"] == "captcha_timeout_mute_permanent")
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM pending_verifications WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check("超时后删除待验证记录", row is None)
    return ok


async def test_worker_mute_restrict_failure():
    await fresh_db()
    await seed_captcha("mute_permanent")
    bot = FakeBot(fail_restrict=True)
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    ok = True
    ok &= check("restrict 失败时不抛异常、不发通知",
                bot.sent == [] and bot.restricts == [])
    return ok


async def test_worker_kick_ban_unchanged():
    ok = True
    await fresh_db()
    await seed_captcha("kick")
    bot = FakeBot()
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    ok &= check("kick 仍是 ban+unban",
                len(bot.bans) == 1 and len(bot.unbans) == 1)
    ok &= check("kick 不发通知", bot.sent == [])
    ok &= check("kick 不调 restrict", bot.restricts == [])

    await fresh_db()
    await seed_captcha("ban")
    bot = FakeBot()
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    ok &= check("ban 仍是只 ban 不 unban",
                len(bot.bans) == 1 and bot.unbans == [])
    ok &= check("ban 不发通知", bot.sent == [])
    ok &= check("ban 不调 restrict", bot.restricts == [])
    return ok


async def test_prompt_text_branches():
    ok = True
    user = User(id=USER_ID, is_bot=False, first_name="小明", username="xiaoming")
    for action, expect in (
        ("kick", "移出群聊"),
        ("ban", "封禁拉黑"),
        ("mute_permanent", "永久禁言（可联系管理员解除）"),
    ):
        await fresh_db()
        await seed_captcha(action)
        bot = FakeBot()
        await handle_new_member_join(bot, GROUP, "拥抱A i", user)
        await cancel_timeout_tasks()
        texts = [s["text"] for s in bot.sent]
        hit = any(expect in t and "超时未完成验证" in t for t in texts)
        ok &= check(f"验证卡片提示 {action}→{expect}", hit)
    return ok


def test_panel_option():
    ok = True
    with open(
        os.path.join(os.path.dirname(__file__), "..",
                     "app/web/templates/index.html"),
        encoding="utf-8",
    ) as f:
        html_text = f.read()
    ok &= check("面板下拉框有 mute_permanent 选项",
                'value="mute_permanent"' in html_text
                and "永久禁言（不移除，可联系管理员解除）" in html_text)
    ok &= check("面板实时预览三分支含永久禁言",
                "mute_permanent' ? '永久禁言（可联系管理员解除）'" in html_text)
    return ok


async def main():
    results = []
    results.append(await test_payload_validation())
    results.append(await test_worker_mute_permanent())
    results.append(await test_worker_mute_restrict_failure())
    results.append(await test_worker_kick_ban_unchanged())
    results.append(await test_prompt_text_branches())
    results.append(test_panel_option())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
