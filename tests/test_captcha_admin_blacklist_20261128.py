# -*- coding: utf-8 -*-
"""入群验证管理员代通过 + 通知艾特用户名 + 验证黑名单（v4.3）测试。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \\
    ~/workspace/.venv-bot-test/bin/python tests/test_captcha_admin_blacklist_20261128.py

覆盖：
- 验证卡片三种题型的键盘都追加一行“👮 管理员验证”按钮（capadmin: 前缀），
  原答题按钮不动；
- 管理员点代通过：pending 删除、权限恢复、欢迎语触发、超时协程取消、
  审计记 captcha_admin_approved；创建者同样放行；普通成员点被拒；
  已过期点提示；
- 超时禁言通知 mention 文字是用户名不是 id，取不到名字回退“用户 <id>”；
- mute_permanent 写黑名单、重复禁言只一条、kick/ban 不写；
- 黑名单接口：列表、解除（恢复权限+删行）、不在表里 404、
  Bot 离线 503、Bot 执行失败 500 且保留行；
- 面板：验证黑名单模块（HUB/allViews/视图/加载渲染/解除/ mock/空状态）。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

os.environ["DATABASE_PATH"] = "/tmp/test_captcha_admin_blacklist.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.types import User

import app.bot.handlers.captcha as captcha
from app.bot.handlers.captcha import (
    RESTRICTED_PERMISSIONS,
    UNRESTRICTED_PERMISSIONS,
    _build_captcha_challenge,
    _verification_timeout_worker,
    on_captcha_admin_approve,
    on_captcha_callback,
)
from app.database.db import close_db, get_db, init_db

GROUP = -1004496842671
USER_ID = 777888999
ADMIN_ID = 111222333
MEMBER_ID = 444555666


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


class FakeBot:
    """只记录调用，不发真请求。"""

    def __init__(self, fail_restrict=False, fail_get_member=False):
        self.fail_restrict = fail_restrict
        self.fail_get_member = fail_get_member
        self.member_status = {}
        self.member_users = {}
        self.restricts = []
        self.bans = []
        self.unbans = []
        self.sent = []
        self.deleted = []
        self._msg_seq = 100

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        if self.fail_get_member:
            raise RuntimeError("no such member")
        status = self.member_status.get(user_id, "member")
        user = self.member_users.get(user_id) or User(
            id=user_id, is_bot=False, first_name=f"用户{user_id}"
        )
        return SimpleNamespace(status=status, user=user)

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


class FakeQuery:
    def __init__(self, data, from_user):
        self.data = data
        self.from_user = from_user
        self.answers = []

    async def answer(self, text, show_alert=False, **kwargs):
        self.answers.append({"text": text, "show_alert": show_alert})
        return True


async def fresh_db():
    await close_db()
    if os.path.exists("/tmp/test_captcha_admin_blacklist.db"):
        os.remove("/tmp/test_captcha_admin_blacklist.db")
    await init_db()
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO group_settings (group_id, group_title, is_active)"
            " VALUES (?, ?, 1)",
            (GROUP, "拥抱A i"),
        )


async def seed_captcha(action="mute_permanent", timeout=60, auto_delete=True):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO group_captcha_settings"
            " (group_id, enabled, captcha_type, timeout, action_on_fail,"
            "  auto_delete_prompt) VALUES (?, 1, 'math', ?, ?, ?)",
            (GROUP, timeout, action, 1 if auto_delete else 0),
        )


async def seed_pending(user_id=USER_ID, message_id=777, expected="23"):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_verifications"
            " (group_id, user_id, message_id, expected_answer, expire_at)"
            " VALUES (?, ?, ?, ?, '2099-01-01T00:00:00+00:00')",
            (GROUP, user_id, message_id, expected),
        )


async def seed_welcome():
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO group_welcome_settings"
            " (group_id, enabled, active_template_id, delete_ttl)"
            " VALUES (?, 1, 1, 0)",
            (GROUP,),
        )


async def cancel_timeout_tasks():
    for task in list(captcha._active_tasks.values()):
        task.cancel()
    await asyncio.sleep(0)


def test_admin_button_in_keyboards():
    ok = True
    for ctype, answer_rows in (("math", 1), ("click", 1), ("choice", 2)):
        _, _, kb = _build_captcha_challenge(ctype, GROUP, USER_ID)
        rows = kb.inline_keyboard
        last = rows[-1]
        ok &= check(
            f"{ctype} 键盘最后一行是管理员验证按钮",
            len(last) == 1
            and last[0].text == "👮 管理员验证"
            and last[0].callback_data == f"capadmin:{GROUP}:{USER_ID}",
        )
        ok &= check(
            f"{ctype} 原答题按钮行数不变",
            len(rows) == answer_rows + 1,
        )
    _, expected, kb = _build_captcha_challenge("math", GROUP, USER_ID)
    first_row = kb.inline_keyboard[0]
    ok &= check(
        "math 答题按钮仍是 4 个且含正确答案",
        len(first_row) == 4
        and any(b.callback_data == f"cap:{GROUP}:{USER_ID}:{expected}" for b in first_row),
    )
    return ok


async def test_admin_approve_success():
    await fresh_db()
    await seed_captcha("kick")
    await seed_pending()
    await seed_welcome()
    bot = FakeBot()
    bot.member_status[ADMIN_ID] = "administrator"
    bot.member_users[USER_ID] = User(
        id=USER_ID, is_bot=False, first_name="小明", username="xiaoming"
    )
    dummy = asyncio.create_task(asyncio.sleep(60))
    captcha._active_tasks[(GROUP, USER_ID)] = dummy
    q = FakeQuery(
        f"capadmin:{GROUP}:{USER_ID}",
        User(id=ADMIN_ID, is_bot=False, first_name="群主"),
    )
    await on_captcha_admin_approve(q, bot)
    await asyncio.sleep(0)
    ok = True
    ok &= check(
        "管理员代通过提示正确",
        any(a["text"] == "✅ 已代为验证通过！" and not a["show_alert"] for a in q.answers),
    )
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM pending_verifications WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check("管理员代通过后删除 pending", row is None)
    ok &= check(
        "管理员代通过恢复 UNRESTRICTED_PERMISSIONS",
        len(bot.restricts) == 1
        and bot.restricts[0]["permissions"] is UNRESTRICTED_PERMISSIONS,
    )
    ok &= check(
        "超时协程被取消并清理",
        (GROUP, USER_ID) not in captcha._active_tasks and dummy.done(),
    )
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT action_type, detail FROM admin_audit_logs"
            " WHERE group_id = ? ORDER BY log_id DESC LIMIT 1",
            (GROUP,),
        )
    ok &= check(
        "审计记 captcha_admin_approved 且注明管理员",
        row is not None
        and row["action_type"] == "captcha_admin_approved"
        and str(ADMIN_ID) in (row["detail"] or ""),
    )
    ok &= check(
        "管理员代通过后触发欢迎语",
        any("欢迎" in s["text"] for s in bot.sent),
    )
    ok &= check("管理员代通过不调 ban/unban", bot.bans == [] and bot.unbans == [])
    return ok


async def test_admin_approve_creator_and_reject():
    ok = True
    # 创建者同样放行
    await fresh_db()
    await seed_captcha("kick")
    await seed_pending()
    bot = FakeBot()
    bot.member_status[ADMIN_ID] = "creator"
    q = FakeQuery(
        f"capadmin:{GROUP}:{USER_ID}",
        User(id=ADMIN_ID, is_bot=False, first_name="群主"),
    )
    await on_captcha_admin_approve(q, bot)
    ok &= check(
        "群主（creator）代通过放行",
        any(a["text"] == "✅ 已代为验证通过！" for a in q.answers)
        and len(bot.restricts) == 1,
    )
    # 普通成员被拒，pending 保留、不恢复权限
    await fresh_db()
    await seed_captcha("kick")
    await seed_pending()
    bot = FakeBot()
    bot.member_status[MEMBER_ID] = "member"
    q = FakeQuery(
        f"capadmin:{GROUP}:{USER_ID}",
        User(id=MEMBER_ID, is_bot=False, first_name="路人"),
    )
    await on_captcha_admin_approve(q, bot)
    ok &= check(
        "普通成员点代通过被拒并弹窗",
        any(a["text"] == "只有群管理员可以代为验证" and a["show_alert"] for a in q.answers),
    )
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM pending_verifications WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check("被拒后 pending 保留", row is not None)
    ok &= check("被拒后不恢复权限", bot.restricts == [])
    return ok


async def test_admin_approve_expired():
    await fresh_db()
    await seed_captcha("kick")
    bot = FakeBot()
    bot.member_status[ADMIN_ID] = "administrator"
    q = FakeQuery(
        f"capadmin:{GROUP}:{USER_ID}",
        User(id=ADMIN_ID, is_bot=False, first_name="群主"),
    )
    await on_captcha_admin_approve(q, bot)
    ok = check(
        "已过期点代通过提示已过期或已完成",
        any(a["text"] == "该验证已过期或已完成！" and a["show_alert"] for a in q.answers),
    )
    ok &= check("已过期点代通过不恢复权限", bot.restricts == [])
    return ok


async def test_self_answer_still_works():
    await fresh_db()
    await seed_captcha("kick")
    await seed_pending(expected="23")
    bot = FakeBot()
    q = FakeQuery(
        f"cap:{GROUP}:{USER_ID}:23",
        User(id=USER_ID, is_bot=False, first_name="小明"),
    )
    await on_captcha_callback(q, bot)
    ok = True
    ok &= check(
        "本人答对仍通过",
        any(a["text"] == "✅ 验证通过，欢迎入群！" for a in q.answers)
        and len(bot.restricts) == 1
        and bot.restricts[0]["permissions"] is UNRESTRICTED_PERMISSIONS,
    )
    # 非本人点答题仍被挡
    await fresh_db()
    await seed_captcha("kick")
    await seed_pending(expected="23")
    bot = FakeBot()
    q = FakeQuery(
        f"cap:{GROUP}:{USER_ID}:23",
        User(id=MEMBER_ID, is_bot=False, first_name="路人"),
    )
    await on_captcha_callback(q, bot)
    ok &= check(
        "非本人点答题仍被拦截",
        any(a["text"] == "这不是你的验证题！" and a["show_alert"] for a in q.answers)
        and bot.restricts == [],
    )
    return ok


async def test_worker_notification_name():
    await fresh_db()
    await seed_captcha("mute_permanent")
    await seed_pending()
    bot = FakeBot()
    bot.member_users[USER_ID] = User(
        id=USER_ID, is_bot=False, first_name="小明", username="xiaoming"
    )
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    ok = True
    notes = [s for s in bot.sent if "永久禁言" in s["text"]]
    ok &= check("mute_permanent 仍发出禁言通知", len(notes) == 1)
    note = notes[0]["text"] if notes else ""
    ok &= check(
        "通知 mention 文字是用户名",
        f"tg://user?id={USER_ID}" in note and ">小明</a>" in note,
    )
    ok &= check("通知不再用“用户 <id>”做链接文字", "用户 777888999" not in note)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_blacklist WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check(
        "mute_permanent 写黑名单且名字是用户名",
        row is not None
        and row["user_name"] == "小明"
        and row["reason"] == "captcha_timeout",
    )
    return ok


async def test_worker_notification_fallback():
    await fresh_db()
    await seed_captcha("mute_permanent")
    await seed_pending()
    bot = FakeBot(fail_get_member=True)
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    ok = True
    notes = [s for s in bot.sent if "永久禁言" in s["text"]]
    note = notes[0]["text"] if notes else ""
    ok &= check(
        "取不到名字时通知回退“用户 <id>”",
        f"tg://user?id={USER_ID}" in note and "用户 777888999" in note,
    )
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_blacklist WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check(
        "取不到名字时黑名单名字也回退 id",
        row is not None and row["user_name"] == "用户 777888999",
    )
    return ok


async def test_blacklist_write_rules():
    ok = True
    # mute 写一条；重复禁言只一条且更新名字
    await fresh_db()
    await seed_captcha("mute_permanent")
    await seed_pending()
    bot = FakeBot()
    bot.member_users[USER_ID] = User(id=USER_ID, is_bot=False, first_name="小明")
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    bot.member_users[USER_ID] = User(id=USER_ID, is_bot=False, first_name="小明改名")
    await seed_pending()
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM group_blacklist WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check(
        "重复禁言只留一条且名字更新",
        len(rows) == 1 and rows[0]["user_name"] == "小明改名",
    )
    # kick/ban 不写黑名单
    for action in ("kick", "ban"):
        await fresh_db()
        await seed_captcha(action)
        await seed_pending()
        bot = FakeBot()
        await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
        async with get_db() as db:
            rows = await db.fetch(
                "SELECT * FROM group_blacklist WHERE group_id = ?",
                (GROUP,),
            )
        ok &= check(f"{action} 不写黑名单", len(rows) == 0)
    # restrict 失败不写黑名单
    await fresh_db()
    await seed_captcha("mute_permanent")
    await seed_pending()
    bot = FakeBot(fail_restrict=True)
    await _verification_timeout_worker(bot, GROUP, USER_ID, 777, 0)
    async with get_db() as db:
        rows = await db.fetch(
            "SELECT * FROM group_blacklist WHERE group_id = ?",
            (GROUP,),
        )
    ok &= check("restrict 失败不写黑名单", len(rows) == 0)
    return ok


async def test_blacklist_endpoints():
    from fastapi.testclient import TestClient

    from app.api.auth import get_current_user
    from app.main import app

    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 8449446199, "first_name": "T", "username": "t"
    }
    ok = True
    await fresh_db()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_blacklist (group_id, user_id, user_name, reason)"
            " VALUES (?, ?, ?, 'captcha_timeout')",
            (GROUP, USER_ID, "小明"),
        )
    r = client.get(f"/api/group/{GROUP}/blacklist")
    ok &= check("GET 黑名单 200", r.status_code == 200)
    data = r.json()
    ok &= check(
        "GET 黑名单返回 1 条且字段齐全",
        data.get("total") == 1
        and len(data.get("blacklist", [])) == 1
        and data["blacklist"][0]["user_id"] == USER_ID
        and data["blacklist"][0]["user_name"] == "小明"
        and data["blacklist"][0]["reason"] == "captcha_timeout",
    )

    # 解除成功：恢复权限 + 删行
    bot = FakeBot()
    app.state.bot = bot
    r = client.post(f"/api/group/{GROUP}/blacklist/{USER_ID}/unmute")
    ok &= check("POST 解除禁言 200", r.status_code == 200)
    ok &= check(
        "解除时恢复 UNRESTRICTED_PERMISSIONS",
        len(bot.restricts) == 1
        and bot.restricts[0]["permissions"] is UNRESTRICTED_PERMISSIONS,
    )
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_blacklist WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check("解除成功后删行", row is None)
    r = client.get(f"/api/group/{GROUP}/blacklist")
    ok &= check("解除后列表为空", r.json().get("total") == 0)

    # 不在表里 -> 404
    r = client.post(f"/api/group/{GROUP}/blacklist/{USER_ID}/unmute")
    ok &= check("不在黑名单解除返回 404", r.status_code == 404)

    # Bot 离线 -> 503
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_blacklist (group_id, user_id, user_name, reason)"
            " VALUES (?, ?, ?, 'captcha_timeout')",
            (GROUP, USER_ID, "小明"),
        )
    app.state.bot = None
    r = client.post(f"/api/group/{GROUP}/blacklist/{USER_ID}/unmute")
    ok &= check("Bot 离线解除返回 503", r.status_code == 503)

    # Bot 执行失败 -> 500 且保留行
    app.state.bot = FakeBot(fail_restrict=True)
    r = client.post(f"/api/group/{GROUP}/blacklist/{USER_ID}/unmute")
    ok &= check("Bot 执行失败解除返回 500", r.status_code == 500)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM group_blacklist WHERE group_id = ? AND user_id = ?",
            (GROUP, USER_ID),
        )
    ok &= check("Bot 执行失败保留黑名单行", row is not None)
    app.state.bot = None
    app.dependency_overrides.clear()
    return ok


def test_panel_blacklist():
    ok = True
    with open(
        os.path.join(os.path.dirname(__file__), "..",
                     "app/web/templates/index.html"),
        encoding="utf-8",
    ) as f:
        html_text = f.read()
    ok &= check("面板有验证黑名单视图", 'id="view-blacklist"' in html_text)
    ok &= check(
        "HUB 模块有验证黑名单且在入群管理分类",
        "id: 'blacklist'" in html_text
        and "title: '验证黑名单'" in html_text
        and "category: '入群管理'" in html_text,
    )
    ok &= check("allViews 含 blacklist", "'blacklist'" in html_text)
    ok &= check(
        "面板有黑名单加载/渲染/解除函数",
        "function loadBlacklist()" in html_text
        and "function renderBlacklist()" in html_text
        and "function unmuteBlacklist(" in html_text,
    )
    ok &= check(
        "面板黑名单空状态文案",
        "黑名单为空" in html_text,
    )
    ok &= check(
        "面板黑名单行有解除禁言按钮",
        "🔓 解除禁言" in html_text,
    )
    ok &= check(
        "mock 有黑名单数据与解除处理",
        "mockStore.blacklist" in html_text
        and "sub === 'blacklist'" in html_text
        and "/unmute" in html_text,
    )
    return ok


async def main():
    results = []
    results.append(test_admin_button_in_keyboards())
    results.append(await test_admin_approve_success())
    results.append(await test_admin_approve_creator_and_reject())
    results.append(await test_admin_approve_expired())
    results.append(await test_self_answer_still_works())
    results.append(await test_worker_notification_name())
    results.append(await test_worker_notification_fallback())
    results.append(await test_blacklist_write_rules())
    results.append(await test_blacklist_endpoints())
    results.append(test_panel_blacklist())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await cancel_timeout_tasks()
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
