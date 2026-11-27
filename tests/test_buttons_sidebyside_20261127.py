# -*- coding: utf-8 -*-
"""欢迎模板按钮并排 + 上移下移测试。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \\
    ~/workspace/.venv-bot-test/bin/python tests/test_buttons_sidebyside_20261127.py

覆盖（pao 2026-11-28 03:45 要的功能）：
- 后端：TemplatePayload 本来就是 List[List[ButtonItem]]，一行两个按钮的模板
  保存 200，读回的 buttons_json 原样是 [[A, B], [C]]；
- trigger_welcome：TG 键盘第一行 2 个按钮（A、B 并排），第二行 1 个（C），颜色透传；
- 面板首页含新控件：＋ 并排 / ⇄ 互换 / ↑ 上移 / ↓ 下移 / 删除本行，
  以及每行 1–2 个并排的说明文字。
注：上移下移与并排是纯前端 DOM 操作（collectTplButtons/tplPreviewButtons
按新结构读），由 tests/test_tpl_buttons_preview_20261127.js 覆盖预览渲染；
本文件覆盖后端与 TG 发送端。
"""
import asyncio
import json
import os
import sys

os.environ["DATABASE_PATH"] = "/tmp/test_buttons_sidebyside.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.types import User
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.database.db import close_db, ensure_group_initialized, get_db, init_db

GROUP = -1004496842671
USER_A = 111111


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


class FakeBot:
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
    if os.path.exists("/tmp/test_buttons_sidebyside.db"):
        os.remove("/tmp/test_buttons_sidebyside.db")
    await init_db()
    await ensure_group_initialized(GROUP)

    from app.main import app
    client = TestClient(app)
    ok = True

    def as_user(uid):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": uid, "first_name": "T", "username": "t"}

    def save_tpl(uid, payload):
        as_user(uid)
        return client.post(f"/api/group/{GROUP}/templates", json=payload)

    # 1. 一行两个按钮的模板保存 200，读回原样
    buttons = [[{"text": "官网", "url": "https://example.com"},
                {"text": "群规", "url": "https://t.me/x", "style": "danger"}],
               [{"text": "客服", "url": "https://t.me/y", "style": "success"}]]
    r = save_tpl(USER_A, {"template_name": "并排模板", "media_type": "none",
                          "media_source": "", "content": "你好 {mention}",
                          "buttons": buttons})
    ok &= check("并排模板保存 200", r.status_code == 200)
    tid = r.json()["template"]["template_id"]
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT buttons_json FROM message_templates WHERE template_id = ?",
            (tid,))
    ok &= check("buttons_json 存的是 [[A, B], [C]]（按文字/链接/颜色比对，忽略 style:null）",
                [[(b.get("text"), b.get("url"), b.get("style")) for b in row2]
                 for row2 in json.loads(row["buttons_json"])] ==
                [[(b.get("text"), b.get("url"), b.get("style")) for b in row2]
                 for row2 in buttons])

    # 2. trigger_welcome：键盘第一行 2 个并排，第二行 1 个，颜色透传
    from app.bot.handlers.welcome import trigger_welcome

    async def set_active(tid):
        async with get_db() as db:
            await db.execute(
                "UPDATE group_welcome_settings SET enabled = 1, active_template_id = ?,"
                " delete_ttl = 0 WHERE group_id = ?",
                (tid, GROUP),
            )

    bot = FakeBot()
    newcomer = User(id=777, is_bot=False, first_name="新人", username="newbie")
    await set_active(tid)
    await trigger_welcome(bot, GROUP, newcomer)
    ok &= check("并排模板欢迎语发出", len(bot.sent) == 1)
    kb = bot.sent[0]["reply_markup"]
    ok &= check("键盘共 2 行",
                kb is not None and len(kb.inline_keyboard) == 2)
    ok &= check("第一行 2 个按钮并排（官网、群规）",
                kb is not None and [b.text for b in kb.inline_keyboard[0]] == ["官网", "群规"])
    ok &= check("第二行 1 个按钮（客服）",
                kb is not None and [b.text for b in kb.inline_keyboard[1]] == ["客服"])
    ok &= check("并排按钮颜色透传（群规红、客服绿）",
                kb is not None
                and getattr(kb.inline_keyboard[0][1], "style", None) == "danger"
                and getattr(kb.inline_keyboard[1][0], "style", None) == "success")

    # 3. 面板首页含新控件与说明
    r = client.get("/")
    ok &= check("面板首页 200", r.status_code == 200)
    ok &= check("面板含“＋ 并排”按钮", "＋ 并排" in r.text)
    ok &= check("面板含“⇄ 互换”按钮", "⇄ 互换" in r.text)
    ok &= check("面板含“↑ 上移 / ↓ 下移”按钮",
                "↑ 上移" in r.text and "↓ 下移" in r.text)
    ok &= check("面板含“删除本行”按钮", "删除本行" in r.text)
    ok &= check("面板说明每行 1–2 个并排", "每行 1–2 个并排" in r.text)
    ok &= check("面板含按钮位结构（tpl-btn-slot）", "tpl-btn-slot" in r.text)

    print("ALL PASS" if ok else "SOME FAILED")
    await close_db()
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
