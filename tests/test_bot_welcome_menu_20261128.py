# -*- coding: utf-8 -*-
"""机器人私聊欢迎语 + 底部菜单（v4.1）测试。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \\
    ~/workspace/.venv-bot-test/bin/python tests/test_bot_welcome_menu_20261128.py

覆盖：
- 欢迎语默认行内容正确（文字一字不差、默认按钮 webapp）；
- POST 鉴权：8449446199 和 5853665560 放行，其他人 403（欢迎语/菜单都测）；
- 标签配对检查：未闭合 / 多余闭合 -> 400，配对（含 pre/a/tg-spoiler 嵌套）-> 200；
- 按钮矩阵 webapp/url 生成：webapp 用 WEBAPP_BASE_URL、url 带 style；
- 菜单默认 5 个按钮：第一个是“➕ Bot 加入群组”（add_to_group），下面 2×2 测试按钮；
- 菜单匹配：reply_text 回 payload、open_panel 回 web_app 按钮、
  add_to_group 回 https://t.me/<用户名>?startgroup 内联 URL 按钮、
  非菜单文字 / 命令文本 / 菜单停用时不拦截；
- /start 私聊与 /menu：先发欢迎语、再发一条带 reply keyboard 的短消息；
  菜单停用时不发键盘；群聊 /start 分支不动。
"""
import asyncio
import json
import os
import sys

os.environ["DATABASE_PATH"] = "/tmp/test_bot_welcome_menu.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.config import settings
from app.database.db import (
    DEFAULT_BOT_MENU_BUTTONS,
    DEFAULT_BOT_WELCOME_BUTTONS,
    DEFAULT_BOT_WELCOME_TEXT,
    close_db,
    get_db,
    init_db,
)

SUPER = 8449446199
SMALL = 5853665560
OTHER = 333333

EXPECTED_WELCOME_TEXT = (
    "👋 <b>欢迎使用群组自动化管理与本地 AI 交互管家！</b>\n\n"
    "点击下方按钮即可唤起 <b>Telegram Mini App 控制面板</b>，支持：\n"
    "• 🛡️ 敏感词与外链/转发安全拦截\n"
    "• 🧩 入群人机验证（算术/图符/点选）\n"
    "• 💌 多模板欢迎语与防刷屏定时撤回\n"
    "• 🤖 本地 Ollama (qwen2.5:7b) 智能对话配置\n"
    "• ⚡ 紧急全员锁群与配置一键导入导出"
)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


class _Me:
    def __init__(self, username):
        self.username = username
        self.id = 9257427138


class FakeBot:
    def __init__(self, username="Zeus_GAI"):
        self._username = username

    async def get_me(self):
        return _Me(self._username)


class _Chat:
    def __init__(self, type_="private", id=12345):
        self.type = type_
        self.id = id


class _User:
    def __init__(self, id=111, username="tester", first_name="测"):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.is_bot = False

    @property
    def full_name(self):
        return self.first_name


class FakeMessage:
    def __init__(self, text, chat_type="private"):
        self.text = text
        self.caption = None
        self.chat = _Chat(chat_type)
        self.from_user = _User()
        self.message_id = 1
        self.reply_to_message = None
        self.forward_origin = None
        self.sent = []
        self.bot = FakeBot()

    async def answer(self, text, reply_markup=None, parse_mode=None, **kw):
        self.sent.append({"method": "answer", "text": text,
                          "reply_markup": reply_markup, "parse_mode": parse_mode})

    async def reply(self, text, reply_markup=None, parse_mode=None, **kw):
        self.sent.append({"method": "reply", "text": text,
                          "reply_markup": reply_markup, "parse_mode": parse_mode})


def _flat_buttons(matrix):
    return [b for row in matrix for b in row]


async def main():
    await close_db()
    if os.path.exists("/tmp/test_bot_welcome_menu.db"):
        os.remove("/tmp/test_bot_welcome_menu.db")
    await init_db()

    from app.main import app
    client = TestClient(app)
    ok = True

    def as_user(uid):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": uid, "first_name": "T", "username": "t"}

    # 1. 欢迎语默认行内容正确
    async with get_db() as db:
        w = await db.fetchrow("SELECT * FROM bot_welcome_settings WHERE id = ?", (1,))
        m = await db.fetchrow("SELECT * FROM bot_menu_settings WHERE id = ?", (1,))
    ok &= check("默认欢迎语文字与旧写死文案一字不差",
                w is not None and w["text"] == EXPECTED_WELCOME_TEXT)
    ok &= check("db.py 默认常量与任务给定文案一致",
                DEFAULT_BOT_WELCOME_TEXT == EXPECTED_WELCOME_TEXT)
    ok &= check("默认欢迎语按钮是 webapp 单按钮",
                w is not None
                and json.loads(w["buttons_json"]) == DEFAULT_BOT_WELCOME_BUTTONS
                and json.loads(w["buttons_json"]) == [
                    [{"text": "🎛️ 打开群管可视化控制面板 (Mini App)",
                      "kind": "webapp"}]])
    ok &= check("默认菜单启用", m is not None and int(m["enabled"]) == 1)
    menu_default = json.loads(m["buttons_json"]) if m else []
    ok &= check("菜单默认 5 个按钮", len(_flat_buttons(menu_default)) == 5)
    ok &= check("菜单默认第一行单独一个加群按钮",
                len(menu_default) == 3 and len(menu_default[0]) == 1
                and menu_default[0][0]["text"] == "➕ Bot 加入群组"
                and menu_default[0][0]["action"] == "add_to_group")
    ok &= check("菜单默认下面 2×2 测试按钮",
                len(menu_default[1]) == 2 and len(menu_default[2]) == 2
                and [b["text"] for b in _flat_buttons(menu_default[1:])] ==
                ["测试1", "测试2", "测试3", "测试4"]
                and all(b["action"] == "reply_text" and b["payload"] == "测试"
                        for b in _flat_buttons(menu_default[1:])))
    ok &= check("db.py 默认菜单常量与库里默认行一致",
                DEFAULT_BOT_MENU_BUTTONS == menu_default)

    # 2. GET 端点返回默认内容
    as_user(OTHER)
    r = client.get("/api/bot-welcome")
    ok &= check("GET /api/bot-welcome 200", r.status_code == 200)
    ok &= check("GET 欢迎语 id=1 且文字正确",
                r.json()["bot_welcome"]["id"] == 1
                and r.json()["bot_welcome"]["text"] == EXPECTED_WELCOME_TEXT)
    r = client.get("/api/bot-menu")
    ok &= check("GET /api/bot-menu 200", r.status_code == 200)
    ok &= check("GET 菜单默认 5 个按钮",
                len(_flat_buttons(r.json()["bot_menu"]["buttons"])) == 5
                and r.json()["bot_menu"]["enabled"] == 1)

    # 3. POST 鉴权：两个号放行，其他人 403（欢迎语 + 菜单）
    for uid in (SUPER, SMALL):
        as_user(uid)
        r = client.post("/api/bot-welcome",
                        json={"text": "你好 <b>世界</b>",
                              "buttons": [[{"text": "面板", "kind": "webapp"}]]})
        ok &= check(f"欢迎语 POST 用户 {uid} 放行 200", r.status_code == 200)
        r = client.post("/api/bot-menu",
                        json={"enabled": True, "buttons": [[
                            {"text": "➕ Bot 加入群组", "action": "add_to_group",
                             "payload": ""}]]})
        ok &= check(f"菜单 POST 用户 {uid} 放行 200", r.status_code == 200)
    as_user(OTHER)
    r = client.post("/api/bot-welcome",
                    json={"text": "别人想改", "buttons": []})
    ok &= check("欢迎语 POST 其他人 403", r.status_code == 403)
    r = client.post("/api/bot-menu",
                    json={"enabled": False, "buttons": []})
    ok &= check("菜单 POST 其他人 403", r.status_code == 403)
    as_user(SUPER)
    r = client.get("/api/bot-welcome")
    ok &= check("403 后欢迎语内容没被改掉",
                r.json()["bot_welcome"]["text"] == "你好 <b>世界</b>")

    # 4. 标签配对检查
    as_user(SUPER)
    r = client.post("/api/bot-welcome",
                    json={"text": "你好 <b>世界", "buttons": []})
    ok &= check("未闭合 <b> 返回 400", r.status_code == 400)
    r = client.post("/api/bot-welcome",
                    json={"text": "你好世界</b>", "buttons": []})
    ok &= check("多余闭合 </b> 返回 400", r.status_code == 400)
    r = client.post("/api/bot-welcome",
                    json={"text": "<b><i>嵌套错</b></i>", "buttons": []})
    ok &= check("交叉嵌套返回 400", r.status_code == 400)
    r = client.post("/api/bot-welcome",
                    json={"text": "看 <a href=\"https://t.me/x\">链接</a> 和"
                                  " <pre>代码</pre> <tg-spoiler>剧透</tg-spoiler>",
                          "buttons": []})
    ok &= check("配对标签（含 a/pre/tg-spoiler）保存 200", r.status_code == 200)
    r = client.post("/api/bot-welcome",
                    json={"text": "x <br> y", "buttons": []})
    ok &= check("不在标签集里的 <br> 不影响保存", r.status_code == 200)
    r = client.post("/api/bot-welcome",
                    json={"text": "x", "buttons": [[
                        {"text": "外链", "kind": "url", "url": ""}]]})
    ok &= check("url 按钮没填链接返回 400", r.status_code == 400)
    r = client.post("/api/bot-welcome",
                    json={"text": "x", "buttons": [[
                        {"text": "坏", "kind": "popup"}]]})
    ok &= check("非法 kind 返回 422", r.status_code == 422)
    r = client.post("/api/bot-menu",
                    json={"enabled": True, "buttons": [[
                        {"text": "坏", "action": "do_something", "payload": ""}]]})
    ok &= check("非法菜单 action 返回 422", r.status_code == 422)
    r = client.post("/api/bot-menu",
                    json={"enabled": True, "buttons": [[
                        {"text": "➕ Bot 加入群组", "action": "add_to_group",
                         "payload": ""}]]})
    ok &= check("add_to_group action 保存 200", r.status_code == 200)

    # 5. POST 持久化：GET 读到新内容
    as_user(SMALL)
    r = client.post("/api/bot-welcome",
                    json={"text": "小号改的欢迎语",
                          "buttons": [[{"text": "面板", "kind": "webapp"},
                                       {"text": "官网", "kind": "url",
                                        "url": "https://example.com",
                                        "style": "danger"}]]})
    ok &= check("小号改欢迎语 200", r.status_code == 200)
    r = client.get("/api/bot-welcome")
    ok &= check("GET 读到小号改后的欢迎语",
                r.json()["bot_welcome"]["text"] == "小号改的欢迎语"
                and len(r.json()["bot_welcome"]["buttons"][0]) == 2)
    # GET 无行时返回默认（不 500）
    async with get_db() as db:
        await db.execute("DELETE FROM bot_welcome_settings WHERE id = ?", (1,))
        await db.execute("DELETE FROM bot_menu_settings WHERE id = ?", (1,))
    r = client.get("/api/bot-welcome")
    ok &= check("删行后 GET 欢迎语仍 200 且回默认",
                r.status_code == 200
                and r.json()["bot_welcome"]["text"] == EXPECTED_WELCOME_TEXT)
    r = client.get("/api/bot-menu")
    ok &= check("删行后 GET 菜单仍 200 且回默认 5 按钮",
                r.status_code == 200
                and len(_flat_buttons(r.json()["bot_menu"]["buttons"])) == 5)
    await init_db()  # 恢复默认行
    async with get_db() as db:
        w = await db.fetchrow("SELECT text FROM bot_welcome_settings WHERE id = ?",
                              (1,))
    ok &= check("init_db 幂等恢复默认欢迎语行", w["text"] == EXPECTED_WELCOME_TEXT)

    # 6. 私聊 /start：欢迎语按钮矩阵 webapp/url 生成
    from app.bot.handlers.moderation import cmd_open_panel, cmd_show_menu
    from app.config import settings as _s

    as_user(SUPER)
    client.post("/api/bot-menu", json={"enabled": True, "buttons": json.loads(
        json.dumps(DEFAULT_BOT_MENU_BUTTONS))})
    client.post("/api/bot-welcome",
                json={"text": "自定义欢迎 <b>语</b>",
                      "buttons": [[{"text": "面板", "kind": "webapp"},
                                   {"text": "官网", "kind": "url",
                                    "url": "https://example.com",
                                    "style": "danger"}]]})
    msg = FakeMessage("/start", "private")
    await cmd_open_panel(msg)
    ok &= check("私聊 /start 发欢迎语", len(msg.sent) >= 1
                and msg.sent[0]["text"] == "自定义欢迎 <b>语</b>")
    ok &= check("欢迎语用 HTML 解析", msg.sent[0]["parse_mode"] == "HTML")
    kb = msg.sent[0]["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row] if kb else []
    ok &= check("欢迎语两个按钮都在一行", len(btns) == 2)
    webapp_btn = next((b for b in btns if b.text == "面板"), None)
    url_btn = next((b for b in btns if b.text == "官网"), None)
    ok &= check("webapp 按钮用 WEBAPP_BASE_URL",
                webapp_btn is not None
                and webapp_btn.web_app is not None
                and webapp_btn.web_app.url == _s.WEBAPP_BASE_URL)
    ok &= check("url 按钮带链接与 style",
                url_btn is not None and url_btn.url == "https://example.com"
                and getattr(url_btn, "style", None) == "danger")
    ok &= check("私聊 /start 第二条是菜单短消息",
                len(msg.sent) == 2 and msg.sent[1]["text"] == "👇 点下方菜单使用功能")
    menu_kb = msg.sent[1]["reply_markup"]
    menu_texts = [b.text for row in menu_kb.keyboard for b in row] if menu_kb else []
    ok &= check("菜单键盘含加群按钮在第一位",
                menu_texts and menu_texts[0] == "➕ Bot 加入群组"
                and len(menu_texts) == 5)
    ok &= check("菜单键盘 resize 且非一次性",
                menu_kb is not None and menu_kb.resize_keyboard is True
                and menu_kb.one_time_keyboard is False)

    # 7. 无行/按钮空时回退默认，永不发空
    async with get_db() as db:
        await db.execute(
            "UPDATE bot_welcome_settings SET text = '', buttons_json = '[]'"
            " WHERE id = ?", (1,))
    msg = FakeMessage("/start", "private")
    await cmd_open_panel(msg)
    ok &= check("欢迎语被清空后回退默认文字",
                msg.sent and msg.sent[0]["text"] == EXPECTED_WELCOME_TEXT)
    ok &= check("回退后仍有默认 webapp 按钮",
                msg.sent and msg.sent[0]["reply_markup"] is not None
                and msg.sent[0]["reply_markup"].inline_keyboard[0][0].web_app
                is not None)
    async with get_db() as db:
        await db.execute(
            "UPDATE bot_welcome_settings SET text = ?, buttons_json = ?"
            " WHERE id = ?",
            (EXPECTED_WELCOME_TEXT,
             json.dumps(DEFAULT_BOT_WELCOME_BUTTONS, ensure_ascii=False), 1))

    # 8. /menu：私聊重发欢迎语 + 菜单键盘；群聊不响应
    msg = FakeMessage("/menu", "private")
    await cmd_show_menu(msg)
    ok &= check("/menu 私聊发两条消息",
                len(msg.sent) == 2
                and msg.sent[1]["text"] == "👇 点下方菜单使用功能")
    msg = FakeMessage("/menu", "group")
    await cmd_show_menu(msg)
    ok &= check("/menu 群聊不发消息", len(msg.sent) == 0)

    # 9. 菜单停用时不发键盘
    as_user(SUPER)
    client.post("/api/bot-menu", json={"enabled": False, "buttons": [[
        {"text": "测试1", "action": "reply_text", "payload": "测试"}]]})
    msg = FakeMessage("/start", "private")
    await cmd_open_panel(msg)
    ok &= check("菜单停用时 /start 只发欢迎语一条",
                len(msg.sent) == 1)
    client.post("/api/bot-menu", json={"enabled": True, "buttons": json.loads(
        json.dumps(DEFAULT_BOT_MENU_BUTTONS))})

    # 10. 群聊 /start 分支一字不动
    msg = FakeMessage("/start", "group")
    await cmd_open_panel(msg)
    ok &= check("群聊 /start 仍提示去私聊打开面板",
                len(msg.sent) == 1
                and "请点击下方按钮在私聊中打开" in msg.sent[0]["text"]
                and msg.sent[0]["reply_markup"].inline_keyboard[0][0].url
                == "https://t.me/Zeus_GAI?start=settings")

    # 11. 菜单匹配逻辑
    from app.bot.handlers.llm_chat import check_bot_menu_reply

    bot = FakeBot(username="Zeus_GAI")
    msg = FakeMessage("测试1", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("reply_text 命中返回 True", hit is True)
    ok &= check("reply_text 回 payload",
                len(msg.sent) == 1 and msg.sent[0]["text"] == "测试")

    # open_panel 按钮：先在菜单里加一个（保留加群与测试按钮）
    async with get_db() as db:
        await db.execute(
            "UPDATE bot_menu_settings SET buttons_json = ? WHERE id = ?",
            (json.dumps([
                [{"text": "打开面板", "action": "open_panel", "payload": ""}],
                [{"text": "➕ Bot 加入群组", "action": "add_to_group",
                  "payload": ""}],
                [{"text": "测试1", "action": "reply_text", "payload": "测试"}],
            ], ensure_ascii=False), 1))
    msg = FakeMessage("打开面板", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("open_panel 命中返回 True", hit is True)
    kb = msg.sent[0]["reply_markup"] if msg.sent else None
    btn = kb.inline_keyboard[0][0] if kb else None
    ok &= check("open_panel 回 web_app 按钮",
                btn is not None and btn.web_app is not None
                and btn.web_app.url == _s.WEBAPP_BASE_URL
                and btn.text == "🎛️ 打开群管可视化控制面板 (Mini App)")

    # add_to_group 按钮
    msg = FakeMessage("➕ Bot 加入群组", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("add_to_group 命中返回 True", hit is True)
    kb = msg.sent[0]["reply_markup"] if msg.sent else None
    btn = kb.inline_keyboard[0][0] if kb else None
    ok &= check("add_to_group 回内联 URL 按钮",
                btn is not None and btn.url is not None)
    ok &= check("加群按钮 URL 是 https://t.me/<用户名>?startgroup 格式",
                btn is not None
                and btn.url == "https://t.me/Zeus_GAI?startgroup")
    ok &= check("加群按钮文字是选群提示",
                btn is not None and btn.text == "选择群组，把 Bot 拉进去")

    # 非菜单文字 / 命令 / 群聊不拦截
    msg = FakeMessage("今天天气怎么样", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("非菜单文字不拦截", hit is False and len(msg.sent) == 0)
    msg = FakeMessage("测试", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("payload 本身不是按钮文字，不拦截",
                hit is False and len(msg.sent) == 0)
    msg = FakeMessage("/start", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("命令文本不拦截", hit is False and len(msg.sent) == 0)
    msg = FakeMessage("测试1", "group")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("群聊不做菜单匹配", hit is False and len(msg.sent) == 0)

    # 菜单停用时不匹配
    async with get_db() as db:
        await db.execute(
            "UPDATE bot_menu_settings SET enabled = 0 WHERE id = ?", (1,))
    msg = FakeMessage("测试1", "private")
    hit = await check_bot_menu_reply(msg, bot)
    ok &= check("菜单停用时不匹配", hit is False and len(msg.sent) == 0)
    async with get_db() as db:
        await db.execute(
            "UPDATE bot_menu_settings SET enabled = 1, buttons_json = ?"
            " WHERE id = ?",
            (json.dumps(DEFAULT_BOT_MENU_BUTTONS, ensure_ascii=False), 1))

    # 12. 管道集成：私聊菜单文字在 LLM 之前被拦截
    from app.bot.handlers.llm_chat import group_message_pipeline

    msg = FakeMessage("测试1", "private")
    await group_message_pipeline(msg, bot)
    ok &= check("管道里私聊菜单文字只回 payload 就结束",
                len(msg.sent) == 1 and msg.sent[0]["text"] == "测试")
    msg = FakeMessage("➕ Bot 加入群组", "private")
    await group_message_pipeline(msg, bot)
    kb = msg.sent[0]["reply_markup"] if msg.sent else None
    btn = kb.inline_keyboard[0][0] if kb else None
    ok &= check("管道里加群按钮也走菜单分支",
                len(msg.sent) == 1 and btn is not None
                and btn.url == "https://t.me/Zeus_GAI?startgroup")

    print("ALL PASS" if ok else "SOME FAILED")
    await close_db()
    return ok


if __name__ == "__main__":
    passed = asyncio.run(main())
    sys.exit(0 if passed else 1)
