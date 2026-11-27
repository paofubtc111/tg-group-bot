# -*- coding: utf-8 -*-
"""欢迎模板按管理员隔离 + 按钮颜色 + 富文本标签测试。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \\
    ~/workspace/.venv-bot-test/bin/python tests/test_templates_isolation_20261127.py

覆盖（pao 2026-11-27 定，中档隔离）：
- 全局模板只有超级管理员 (8449446199) 能改，其他人改 -> 403；
- 新模板归属创建者，他人看不见也改不了（403），删也 403；
- 旧模板（owner 为 NULL）本群管理员仍可见可改；
- active_template_id 不能设成设置者看不见的模板（403）；
- ButtonItem style：合法值通过、非法值 422；无 style 的旧按钮行为不变；
- welcome.py：style 透传到 InlineKeyboardButton；非法 style 回落 None；
  buttons_json 解析失败时键盘丢弃、欢迎语照发。
"""
import asyncio
import json
import os
import sys

os.environ["DATABASE_PATH"] = "/tmp/test_templates_isolation.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.types import User
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.database.db import close_db, ensure_group_initialized, get_db, init_db

GROUP = -1004496842671
SUPER = 8449446199
USER_A = 111111
USER_B = 222222


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


class FakeBot:
    """只记录发送，不发真请求。"""

    def __init__(self):
        self.sent = []

    async def get_chat(self, chat_id):
        class C:
            title = "测试群"

        return C()

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None,
                           disable_web_page_preview=None):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "reply_markup": reply_markup, "parse_mode": parse_mode})

        class M:
            message_id = 99

        return M()

    async def send_photo(self, *a, **k):
        raise AssertionError("本测试不应走 send_photo")

    async def send_animation(self, *a, **k):
        raise AssertionError("本测试不应走 send_animation")

    async def delete_message(self, *a, **k):
        pass


async def main():
    await close_db()
    if os.path.exists("/tmp/test_templates_isolation.db"):
        os.remove("/tmp/test_templates_isolation.db")
    await init_db()
    await ensure_group_initialized(GROUP)

    from app.main import app
    client = TestClient(app)
    ok = True

    def as_user(uid):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": uid, "first_name": "T", "username": "t"}

    def tpl_list(uid):
        as_user(uid)
        r = client.get(f"/api/group/{GROUP}/templates")
        assert r.status_code == 200, r.text
        return r.json()["templates"]

    def save_tpl(uid, payload):
        as_user(uid)
        return client.post(f"/api/group/{GROUP}/templates", json=payload)

    # 0. 全局模板存在，owner 为空
    async with get_db() as db:
        g = await db.fetchrow(
            "SELECT template_id, group_id, owner_user_id FROM message_templates "
            "WHERE group_id IS NULL")
    ok &= check("全局模板存在且 owner 为空", bool(g) and g["owner_user_id"] is None)
    gid_global = g["template_id"]

    # 1. 超级管理员能改全局模板
    rows = tpl_list(SUPER)
    grow = [t for t in rows if t["template_id"] == gid_global]
    ok &= check("超级管理员看得到全局模板", len(grow) == 1)
    ok &= check("超级管理员对全局模板 editable=true",
                bool(grow) and grow[0].get("editable") is True)
    r = save_tpl(SUPER, {"template_id": gid_global, "template_name": "系统默认入群欢迎模板",
                         "media_type": "none", "media_source": "",
                         "content": "超级管理员改过", "buttons": []})
    ok &= check("超级管理员改全局模板 200", r.status_code == 200)

    # 2. 普通管理员改全局模板 -> 403，且列表里 editable=false
    rows = tpl_list(USER_A)
    grow = [t for t in rows if t["template_id"] == gid_global]
    ok &= check("普通管理员看得到全局模板", len(grow) == 1)
    ok &= check("普通管理员对全局模板 editable=false",
                bool(grow) and grow[0].get("editable") is False)
    r = save_tpl(USER_A, {"template_id": gid_global, "template_name": "系统默认入群欢迎模板",
                          "media_type": "none", "media_source": "",
                          "content": "普通人想改全局", "buttons": []})
    ok &= check("普通管理员改全局模板 403", r.status_code == 403)
    async with get_db() as db:
        g2 = await db.fetchrow(
            "SELECT content FROM message_templates WHERE template_id = ?", (gid_global,))
    ok &= check("全局模板内容没被普通人改掉", g2["content"] == "超级管理员改过")

    # 3. 新模板归属创建者
    r = save_tpl(USER_A, {"template_name": "A 的模板 1", "media_type": "none",
                          "media_source": "", "content": "A 的内容", "buttons": []})
    ok &= check("A 新建模板 200", r.status_code == 200)
    tpl_a1 = r.json()["template"]["template_id"]
    ok &= check("新建模板 owner_user_id=创建者",
                r.json()["template"].get("owner_user_id") == USER_A)
    rows = tpl_list(USER_A)
    mine = [t for t in rows if t["template_id"] == tpl_a1]
    ok &= check("A 看得到自己的模板且 editable=true",
                len(mine) == 1 and mine[0].get("editable") is True)
    r = save_tpl(USER_A, {"template_id": tpl_a1, "template_name": "A 的模板 1",
                          "media_type": "none", "media_source": "",
                          "content": "A 改自己的", "buttons": []})
    ok &= check("A 改自己的模板 200", r.status_code == 200)

    # 4. 他人看不见、改不了、删不了 A 的模板
    rows = tpl_list(USER_B)
    ok &= check("B 的列表里没有 A 的模板",
                all(t["template_id"] != tpl_a1 for t in rows))
    r = save_tpl(USER_B, {"template_id": tpl_a1, "template_name": "A 的模板 1",
                          "media_type": "none", "media_source": "",
                          "content": "B 想改 A 的", "buttons": []})
    ok &= check("B 改 A 的模板 403", r.status_code == 403)
    as_user(USER_B)
    r = client.delete(f"/api/group/{GROUP}/templates/{tpl_a1}")
    ok &= check("B 删 A 的模板 403", r.status_code == 403)
    as_user(USER_A)
    r = client.delete(f"/api/group/{GROUP}/templates/{tpl_a1}")
    ok &= check("A 删自己的模板 200", r.status_code == 200)
    rows = tpl_list(USER_A)
    ok &= check("删后 A 的列表里没有该模板",
                all(t["template_id"] != tpl_a1 for t in rows))

    # 5. 旧模板（owner 为 NULL）本群管理员仍可见可改
    async with get_db() as db:
        legacy_id = await db.execute(
            "INSERT INTO message_templates (group_id, owner_user_id, template_name,"
            " media_type, media_source, content, buttons_json)"
            " VALUES (?, NULL, ?, 'none', '', ?, '[]')",
            (GROUP, "旧模板（无归属）", "旧内容"),
        )
    rows = tpl_list(USER_B)
    leg = [t for t in rows if t["template_id"] == legacy_id]
    ok &= check("B 看得到旧模板", len(leg) == 1)
    ok &= check("旧模板对 B editable=true",
                bool(leg) and leg[0].get("editable") is True)
    r = save_tpl(USER_B, {"template_id": legacy_id, "template_name": "旧模板（无归属）",
                          "media_type": "none", "media_source": "",
                          "content": "B 改旧模板", "buttons": []})
    ok &= check("B 改旧模板 200", r.status_code == 200)

    # 6. active_template_id 不能设成设置者看不见的模板
    r = save_tpl(USER_A, {"template_name": "A 的模板 2", "media_type": "none",
                          "media_source": "", "content": "A2", "buttons": []})
    tpl_a2 = r.json()["template"]["template_id"]
    as_user(USER_A)
    r = client.post(f"/api/group/{GROUP}/welcome",
                    json={"enabled": True, "active_template_id": tpl_a2,
                          "delete_ttl": 0, "clean_previous": False})
    ok &= check("A 把自己的模板设为欢迎语 200", r.status_code == 200)
    as_user(USER_B)
    r = client.post(f"/api/group/{GROUP}/welcome",
                    json={"enabled": True, "active_template_id": tpl_a2,
                          "delete_ttl": 0, "clean_previous": False})
    ok &= check("B 把 A 的模板设为欢迎语 403", r.status_code == 403)
    r = client.post(f"/api/group/{GROUP}/welcome",
                    json={"enabled": True, "active_template_id": gid_global,
                          "delete_ttl": 0, "clean_previous": False})
    ok &= check("B 把全局模板设为欢迎语 200（全局人人可见）", r.status_code == 200)

    # 7. 导出只含设置者可见的模板
    as_user(USER_B)
    r = client.get(f"/api/group/{GROUP}/export")
    ok &= check("导出 200", r.status_code == 200)
    exp_names = [t["template_name"] for t in r.json().get("templates", [])]
    ok &= check("B 的导出里没有 A 的模板", "A 的模板 2" not in exp_names)
    as_user(USER_A)
    r = client.get(f"/api/group/{GROUP}/export")
    exp_names = [t["template_name"] for t in r.json().get("templates", [])]
    ok &= check("A 的导出里有自己的模板", "A 的模板 2" in exp_names)

    # 8. ButtonItem style：合法值通过、非法值 422、无 style 的旧按钮不变
    r = save_tpl(USER_A, {"template_name": "彩色按钮", "media_type": "none",
                          "media_source": "", "content": "x",
                          "buttons": [[{"text": "红", "url": "https://t.me/x",
                                       "style": "danger"},
                                      {"text": "绿", "url": "https://t.me/y",
                                       "style": "success"}]]})
    ok &= check("带合法 style 的按钮保存 200", r.status_code == 200)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT buttons_json FROM message_templates WHERE template_name = ?",
            ("彩色按钮",))
    stored = json.loads(row["buttons_json"])
    ok &= check("style 存进 buttons_json",
                stored[0][0].get("style") == "danger"
                and stored[0][1].get("style") == "success")
    r = save_tpl(USER_A, {"template_name": "非法颜色", "media_type": "none",
                          "media_source": "", "content": "x",
                          "buttons": [[{"text": "粉", "url": "https://t.me/x",
                                       "style": "pink"}]]})
    ok &= check("非法 style 保存 422", r.status_code == 422)
    r = save_tpl(USER_A, {"template_name": "旧按钮", "media_type": "none",
                          "media_source": "", "content": "x",
                          "buttons": [[{"text": "官网", "url": "https://example.com"}]]})
    ok &= check("无 style 的旧按钮保存 200", r.status_code == 200)

    # 9. welcome.py：style 透传；非法 style 回落 None；坏 JSON 丢键盘但照发
    from app.bot.handlers.welcome import trigger_welcome

    async def set_active(tid):
        async with get_db() as db:
            await db.execute(
                "UPDATE group_welcome_settings SET enabled = 1, active_template_id = ?,"
                " delete_ttl = 0 WHERE group_id = ?",
                (tid, GROUP),
            )

    async def seed_tpl(name, buttons_json, content="你好 {mention}"):
        async with get_db() as db:
            tid = await db.execute(
                "INSERT INTO message_templates (group_id, owner_user_id, template_name,"
                " media_type, media_source, content, buttons_json)"
                " VALUES (?, ?, ?, 'none', '', ?, ?)",
                (GROUP, USER_A, name, content, buttons_json),
            )
        return tid

    bot = FakeBot()
    newcomer = User(id=777, is_bot=False, first_name="新人", username="newbie")

    tid = await seed_tpl("发彩色按钮",
                         json.dumps([[{"text": "红", "url": "https://t.me/x",
                                      "style": "danger"}]]))
    await set_active(tid)
    await trigger_welcome(bot, GROUP, newcomer)
    ok &= check("彩色模板欢迎语发出", len(bot.sent) == 1)
    ok &= check("发送用 HTML 解析", bot.sent[0]["parse_mode"] == "HTML")
    kb = bot.sent[0]["reply_markup"]
    btn = kb.inline_keyboard[0][0] if kb else None
    ok &= check("style 透传到 InlineKeyboardButton",
                btn is not None and getattr(btn, "style", None) == "danger")

    bot = FakeBot()
    tid = await seed_tpl("坏 JSON 按钮", "not json{{")
    await set_active(tid)
    await trigger_welcome(bot, GROUP, newcomer)
    ok &= check("坏 buttons_json 也照发欢迎语", len(bot.sent) == 1)
    ok &= check("坏 buttons_json 时无键盘", bot.sent[0]["reply_markup"] is None)

    bot = FakeBot()
    tid = await seed_tpl("非法颜色按钮",
                         json.dumps([[{"text": "粉", "url": "https://t.me/x",
                                      "style": "pink"}]]))
    await set_active(tid)
    await trigger_welcome(bot, GROUP, newcomer)
    ok &= check("非法颜色按钮也照发", len(bot.sent) == 1)
    kb = bot.sent[0]["reply_markup"]
    btn = kb.inline_keyboard[0][0] if kb else None
    ok &= check("非法 style 回落为 None",
                btn is not None and getattr(btn, "style", None) is None)

    # 10. 面板里有新编辑器与富文本工具
    r = client.get("/")
    ok &= check("面板首页 200", r.status_code == 200)
    ok &= check("面板含按钮结构化编辑器", 'id="tpl-buttons-editor"' in r.text)
    ok &= check("面板含格式工具栏", 'wrapTplFormat' in r.text)
    ok &= check("面板含右键菜单", 'id="tpl-content-menu"' in r.text)
    ok &= check("面板含实时预览", 'id="tpl-content-preview"' in r.text)
    ok &= check("面板含标签配对检查", 'checkTplTagsBalanced' in r.text)

    print("ALL PASS" if ok else "SOME FAILED")
    await close_db()
    return ok


if __name__ == "__main__":
    passed = asyncio.run(main())
    sys.exit(0 if passed else 1)
