"""回归测试: 2026-11-10 pao 新报的 5 个 bug。

用真实 Dispatcher + 假 Bot(不发任何网络请求)+ 临时数据库, 跑真实的
消息管道 (GroupTrackerMiddleware + setup_bot_routers), 复现/验证:

  Bug 1 话术关群: 普通群员发关键词不锁群, 只有管理员发才锁
  Bug 2 群成员: 面板成员列表里只有管理员, 正常群员没识别到
  Bug 3 消息统计: 请求失败 500 (PG 下 GROUP BY 裸列 + daily 键类型)
  Bug 4 Bot 触发: 关键词从"前缀匹配"改成"句中包含即触发"
  Bug 5 改名监控: 没监控上 (中间件先更新档案, 管道再对比永远相等)
另带: 定时关群循环不覆写话术锁; allow_admin_trigger 列存在。

用法:
  TEST_DB_MODE=sqlite /tmp/pgtest/bin/python tests/test_five_bugs_20261110.py
  TEST_DB_MODE=pg     /tmp/pgtest/bin/python tests/test_five_bugs_20261110.py
  TEST_REPO=<另一份代码路径>  可指定要测的代码树 (用于旧代码对照)
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

MODE = os.environ.get("TEST_DB_MODE", "sqlite")
REPO = os.environ.get(
    "TEST_REPO", "/home/hatch/workspace/tg-render-deploy/tg-group-bot"
)
sys.path.insert(0, REPO)

# ---- 环境变量必须在 import app 之前定好 ----
if MODE == "pg":
    import pgserver

    _server = pgserver.get_server("/tmp/pgtest-pgdata-5bugs")
    os.environ["DATABASE_URL"] = _server.get_uri()
    os.environ.pop("DATABASE_PATH", None)
else:
    os.environ.pop("DATABASE_URL", None)
    _db_path = "/tmp/pgtest/five_bugs_test_bot.db"
    if os.path.exists(_db_path):
        os.remove(_db_path)
    os.environ["DATABASE_PATH"] = _db_path

from aiogram import Bot, Dispatcher
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberMember,
    Message,
    Update,
    User,
)

import app.bot.handlers.group_features  # 先加载: 与 main.py 的导入顺序一致, 避免循环 import
from app.api.routes import api_get_group_stats
from app.bot.handlers import setup_bot_routers
from app.bot.handlers.group_features import (
    _apply_scheduled_lockdowns,
    _expire_keyword_locks,
)
from app.bot.handlers.llm_chat import _check_llm_trigger
from app.bot.middlewares.group_tracker import GroupTrackerMiddleware
from app.database.db import close_db, ensure_group_initialized, get_db, init_db

GROUP_ID = -1004496842671
STATS_GID = -1004496842672
GROUP_TITLE = "拥抱A i"
ADMIN_ID = 8449446199
MEMBER_ID = 777001
MEMBER2_ID = 777002
MEMBER3_ID = 777003
BOT_ID = 9257427138

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  PASS  {name} {detail}")
    else:
        failed.append(name)
        print(f"  FAIL  {name} {detail}")


class FakeBot(Bot):
    """不发网络请求的 Bot: 按 user_id 返回管理员/普通成员身份, 记录所有调用。"""

    def __init__(self, admin_ids):
        super().__init__(token="123456789:" + "A" * 35)
        self.admin_ids = set(admin_ids)
        self.calls = []
        self._msg_seq = 1000

    def _admin_member(self, user_id):
        return ChatMemberAdministrator(
            status="administrator",
            user=User(id=user_id, is_bot=False, first_name="Admin", username="admin"),
            can_be_edited=False,
            is_anonymous=False,
            can_manage_chat=True,
            can_delete_messages=True,
            can_manage_video_chats=True,
            can_restrict_members=True,
            can_promote_members=True,
            can_change_info=True,
            can_invite_users=True,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            can_send_welcome_messages=False,
        )

    async def get_me(self, **kwargs):
        return User(id=BOT_ID, is_bot=True, first_name="test2", username="flowerGPbot")

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("get_chat_member", chat_id, user_id))
        if user_id in self.admin_ids:
            return self._admin_member(user_id)
        return ChatMemberMember(
            status="member",
            user=User(id=user_id, is_bot=False, first_name="M", username="m"),
        )

    async def get_chat_administrators(self, chat_id, **kwargs):
        return [self._admin_member(uid) for uid in sorted(self.admin_ids)]

    async def set_chat_permissions(self, chat_id, permissions, **kwargs):
        self.calls.append(
            ("set_chat_permissions", chat_id, permissions.can_send_messages)
        )
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send_message", chat_id, text))
        self._msg_seq += 1
        return Message(
            message_id=self._msg_seq,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="supergroup", title=GROUP_TITLE),
            from_user=User(
                id=BOT_ID, is_bot=True, first_name="test2", username="flowerGPbot"
            ),
            text=text,
        )

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.calls.append(("delete_message", chat_id, message_id))
        return True

    async def restrict_chat_member(self, chat_id, user_id, permissions, **kwargs):
        self.calls.append(("restrict_chat_member", chat_id, user_id))
        return True

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("ban_chat_member", chat_id, user_id))
        return True

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("unban_chat_member", chat_id, user_id))
        return True

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.calls.append(("send_chat_action", chat_id, str(action)))
        return True


def group_chat(gid=GROUP_ID):
    return Chat(id=gid, type="supergroup", title=GROUP_TITLE)


def admin_user():
    return User(id=ADMIN_ID, is_bot=False, first_name="泡芙", username="yujinxiang_u")


def member_user(uid=MEMBER_ID, first_name="小明", username="xiaoming"):
    return User(id=uid, is_bot=False, first_name=first_name, username=username)


def bot_user():
    return User(id=BOT_ID, is_bot=True, first_name="test2", username="flowerGPbot")


def group_message(message_id, text, user):
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=group_chat(),
        from_user=user,
        text=text,
    )


def sent_texts(bot, needle):
    return [c[2] for c in bot.calls if c[0] == "send_message" and needle in c[2]]


async def clear_group_config(gid=GROUP_ID):
    """每项测试开始前清掉该群的可变配置与成员/消息, 互不干扰。"""
    async with get_db() as db:
        for table in (
            "lockdown_keywords",
            "group_sensitive_rules",
            "group_emergency_settings",
            "group_moderation_settings",
            "group_rename_watch",
            "group_llm_settings",
            "group_lock_state",
            "bottom_buttons",
            "keyword_replies",
            "group_force_subscribe",
            "scheduled_lockdowns",
            "group_members",
            "group_messages",
            "user_warn_records",
        ):
            await db.execute(f"DELETE FROM {table} WHERE group_id = ?", (gid,))


async def seed_lockdown_keyword(keyword, allow_admin_trigger=0, lock_minutes=30):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO lockdown_keywords (group_id, keyword, lock_minutes, enabled, allow_admin_trigger) VALUES (?, ?, ?, 1, ?)",
            (GROUP_ID, keyword, lock_minutes, allow_admin_trigger),
        )


async def seed_sensitive_rule(pattern, match_type="contains", punish="delete_only"):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_sensitive_rules (group_id, pattern, match_type, punish_action) VALUES (?, ?, ?, ?)",
            (GROUP_ID, pattern, match_type, punish),
        )


async def seed_emergency(lockdown_mode=1):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_emergency_settings (group_id, lockdown_mode) VALUES (?, ?) ON CONFLICT(group_id) DO UPDATE SET lockdown_mode = excluded.lockdown_mode",
            (GROUP_ID, lockdown_mode),
        )


async def seed_member_row(uid, first_name, username, message_count):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_members (group_id, user_id, username, first_name, message_count, last_seen, joined_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (GROUP_ID, uid, username, first_name, message_count),
        )


async def seed_rename_watch(enabled=1):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_rename_watch (group_id, enabled) VALUES (?, ?) ON CONFLICT(group_id) DO UPDATE SET enabled = excluded.enabled",
            (GROUP_ID, enabled),
        )


async def member_row(uid, gid=GROUP_ID):
    async with get_db() as db:
        return await db.fetchrow(
            "SELECT group_id, user_id, username, first_name, message_count FROM group_members WHERE group_id = ? AND user_id = ?",
            (gid, uid),
        )


async def lock_state(gid=GROUP_ID):
    async with get_db() as db:
        return await db.fetchrow(
            "SELECT group_id, locked, source, locked_until FROM group_lock_state WHERE group_id = ?",
            (gid,),
        )


async def msg_count(uid, gid=GROUP_ID):
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS c FROM group_messages WHERE group_id = ? AND user_id = ?",
            (gid, uid),
        )
        return row["c"] if row else 0


async def main():
    await init_db()
    print(f"[{MODE}] init_db ok")
    await ensure_group_initialized(GROUP_ID, GROUP_TITLE)
    await ensure_group_initialized(STATS_GID, "统计专用群")

    bot = FakeBot(admin_ids={ADMIN_ID})
    dp = Dispatcher()
    dp.update.outer_middleware(GroupTrackerMiddleware())
    dp.include_router(setup_bot_routers())

    msg_seq = [100]

    async def feed(text, user):
        msg_seq[0] += 1
        await dp.feed_update(
            bot, Update(update_id=msg_seq[0], message=group_message(msg_seq[0], text, user))
        )

    # ---------- T1 [Bug 2] 普通群员发一条普通文本 -> 成员登记 + 消息归档 ----------
    print("T1 [Bug2] 普通群员发言 -> group_members + group_messages")
    await clear_group_config()
    await feed("大家好", member_user())
    mrow = await member_row(MEMBER_ID)
    check("普通群员出现在 group_members", mrow is not None, str(mrow))
    check("成员 first_name/username 正确", mrow and mrow["first_name"] == "小明" and mrow["username"] == "xiaoming", str(mrow))
    check("发言计数为 1", mrow and mrow["message_count"] == 1, str(mrow))
    check("消息已归档到 group_messages", (await msg_count(MEMBER_ID)) == 1)

    # ---------- T2 [Bug 1 核心] 话术关群: 普通群员触发, 管理员豁免 ----------
    print("T2 [Bug1核心] 话术关群: 普通群员触发 / 管理员豁免")
    await clear_group_config()
    await seed_lockdown_keyword("关群", allow_admin_trigger=0)
    await feed("大家关群一下", member_user())
    st = await lock_state()
    check("普通群员发关键词 -> 锁群", st and st["locked"] == 1, str(st))
    check("锁来源是 keyword", st and st["source"] == "keyword", str(st))
    check("发了锁群公告", len(sent_texts(bot, "已锁定")) >= 1)
    async with get_db() as db:
        await db.execute("DELETE FROM group_lock_state WHERE group_id = ?", (GROUP_ID,))
    bot.calls.clear()
    await feed("大家关群一下", admin_user())
    st = await lock_state()
    check("管理员发关键词 -> 豁免不锁 (allow_admin_trigger=0)", not st or st["locked"] == 0, str(st))
    check("管理员触发不发锁群公告", len(sent_texts(bot, "已锁定")) == 0)

    # ---------- T3 [Bug 1 机制] 敏感词含关群关键词 -> 普通群员被拦截吃掉 ----------
    print("T3 [Bug1机制] 敏感词命中关群关键词: 普通群员 vs 管理员")
    await clear_group_config()
    await seed_sensitive_rule("关群", "contains", "delete_only")
    await seed_lockdown_keyword("关群", allow_admin_trigger=1)
    bot.calls.clear()
    await feed("关群", member_user())
    st = await lock_state()
    check("普通群员发关键词 (敏感词重叠) -> 仍要锁群", st and st["locked"] == 1, str(st))
    async with get_db() as db:
        await db.execute("DELETE FROM group_lock_state WHERE group_id = ?", (GROUP_ID,))
    bot.calls.clear()
    await feed("关群", admin_user())
    st = await lock_state()
    check("管理员发关键词 (allow_admin_trigger=1) -> 锁群", st and st["locked"] == 1, str(st))

    # ---------- T4 [Bug 1 机制2] 紧急锁群模式开启 -> 普通群员关键词也被吃掉 ----------
    print("T4 [Bug1机制2] 紧急模式开启: 普通群员发关键词")
    await clear_group_config()
    await seed_emergency(lockdown_mode=1)
    await seed_lockdown_keyword("关群", allow_admin_trigger=0)
    bot.calls.clear()
    await feed("关群", member_user())
    st = await lock_state()
    check("紧急模式下普通群员发关键词 -> 仍要锁群", st and st["locked"] == 1, str(st))

    # ---------- T4b 紧急模式下话术锁到期不许把紧急锁也解了 ----------
    print("T4b 紧急模式下话术锁到期 -> 不解锁")
    await clear_group_config()
    await seed_emergency(lockdown_mode=1)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_lock_state (group_id, locked, source, locked_until) VALUES (?, 1, 'keyword', ?)",
            (GROUP_ID, past),
        )
    bot.calls.clear()
    await _expire_keyword_locks(bot)
    st = await lock_state()
    check("紧急模式下过期话术锁不被解锁", st and st["locked"] == 1, str(st))
    check(
        "紧急模式下不发'已自动解锁'公告",
        len(sent_texts(bot, "已自动解锁")) == 0,
    )
    check(
        "紧急模式下不调 set_chat_permissions 解锁",
        all(
            not (c[0] == "set_chat_permissions" and c[2] is True)
            for c in bot.calls
        ),
    )

    # ---------- T5 [Bug 3] 消息统计接口: PG 下 500 + daily 全 0 ----------
    print("T5 [Bug3] 消息统计 api_get_group_stats")
    await clear_group_config(STATS_GID)
    cst_now = datetime.now(timezone(timedelta(hours=8)))
    today_utc = (cst_now - timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    two_days_ago_utc = (cst_now - timedelta(days=2, hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    seed_msgs = [
        (MEMBER_ID, "xiaoming", today_utc),
        (MEMBER_ID, "xiaoming", today_utc),
        (MEMBER_ID, "xiaoming", today_utc),
        (MEMBER_ID, "xiaoming", two_days_ago_utc),
        (MEMBER2_ID, "xiaohong", today_utc),
        (MEMBER2_ID, "xiaohong", today_utc),
        (MEMBER3_ID, "", today_utc),
    ]
    async with get_db() as db:
        for i, (uid, uname, ts) in enumerate(seed_msgs):
            await db.execute(
                "INSERT INTO group_messages (group_id, message_id, user_id, username, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (STATS_GID, 5000 + i, uid, uname, "x", ts),
            )
        for uid, uname, fname in (
            (MEMBER_ID, "xiaoming", "小明"),
            (MEMBER2_ID, "xiaohong", "小红"),
            (MEMBER3_ID, "", "小刚"),
        ):
            await db.execute(
                "INSERT INTO group_members (group_id, user_id, username, first_name, message_count) VALUES (?, ?, ?, ?, 0)",
                (STATS_GID, uid, uname, fname),
            )
    from fastapi import FastAPI, Request

    _app = FastAPI()
    _app.state.bot = bot
    _scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "app": _app}
    stats = None
    stats_err = None
    try:
        stats = await api_get_group_stats(
            STATS_GID,
            Request(_scope),
            {"id": ADMIN_ID, "first_name": "泡芙", "username": "yujinxiang_u"},
        )
    except Exception as exc:
        stats_err = exc
    check("消息统计接口不抛 500", stats is not None, f"err={stats_err!r}")
    if stats is not None:
        today_iso = cst_now.date().isoformat()
        two_iso = (cst_now - timedelta(days=2)).date().isoformat()
        daily = {d["day"]: d["count"] for d in stats["daily"]}
        check("今日计数正确 (6 条)", daily.get(today_iso) == 6, str(daily))
        check("两天前计数正确 (1 条)", daily.get(two_iso) == 1, str(daily))
        check("其余日期为 0", all(v == 0 for k, v in daily.items() if k not in (today_iso, two_iso)), str(daily))
        top = stats["top_senders"]
        check("top_senders 有 3 人", len(top) == 3, str(top))
        check(
            "top_senders 计数/用户名正确 (全时段: 小明 4 条含两天前 1 条)",
            top[0]["cnt"] == 4 and top[0]["username"] == "xiaoming" and top[1]["cnt"] == 2,
            str(top),
        )
        check("total_messages=7", stats["total_messages"] == 7, str(stats["total_messages"]))
        check("total_members=3", stats["total_members"] == 3, str(stats["total_members"]))

    # ---------- T6 [Bug 4] 关键词触发: 句中包含即触发, 不再要求前缀 ----------
    print("T6 [Bug4] _check_llm_trigger 句中包含触发")
    llm_cfg = {
        "enabled": True,
        "trigger_on_mention": True,
        "trigger_on_reply": True,
        "trigger_keywords": "小助手,ai",
    }
    bot_u = User(id=BOT_ID, is_bot=True, first_name="test2", username="flowerGPbot")

    def trig_msg(text, reply_to_bot=False):
        reply = None
        if reply_to_bot:
            reply = Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=group_chat(),
                from_user=bot_u,
                text="bot 说",
            )
        return Message(
            message_id=2,
            date=datetime.now(timezone.utc),
            chat=group_chat(),
            from_user=member_user(),
            text=text,
            reply_to_message=reply,
        )

    ok, cleaned = _check_llm_trigger(trig_msg("你好小助手，今天天气怎么样"), bot_u, llm_cfg)
    check("句中含关键词触发", ok is True, f"cleaned={cleaned!r}")
    check("清洗后去掉句中关键词", ok and "小助手" not in cleaned and "天气" in cleaned, f"cleaned={cleaned!r}")
    ok, cleaned = _check_llm_trigger(trig_msg("小助手你好"), bot_u, llm_cfg)
    check("前缀仍可触发", ok is True, f"cleaned={cleaned!r}")
    ok, _ = _check_llm_trigger(trig_msg("今天天气不错"), bot_u, llm_cfg)
    check("不含关键词不触发", ok is False)
    ok, cleaned = _check_llm_trigger(trig_msg("@flowerGPbot 你好"), bot_u, llm_cfg)
    check("@提及触发不变", ok is True and "你好" in cleaned, f"cleaned={cleaned!r}")
    ok, cleaned = _check_llm_trigger(trig_msg("收到，谢谢", reply_to_bot=True), bot_u, llm_cfg)
    check("回复触发不变", ok is True, f"cleaned={cleaned!r}")
    ok, cleaned = _check_llm_trigger(trig_msg("大家说ai好用"), bot_u, llm_cfg)
    check("关键词 ai 在句中触发 (去关键词后剩'大家说好用')", ok is True and "ai" not in cleaned.lower(), f"cleaned={cleaned!r}")

    # ---------- T7 [Bug 5] 改名监控: 中间件先对比再更新档案 ----------
    print("T7 [Bug5] 改名监控: 旧名成员发新名消息 -> 公告")
    await clear_group_config()
    await seed_rename_watch(enabled=1)
    await seed_member_row(MEMBER_ID, "老名字", "xiaoming", 5)
    bot.calls.clear()
    await feed("大家好", member_user(first_name="新名字", username="xiaoming"))
    ann = sent_texts(bot, "改名监控")
    check("改名监控发了公告", len(ann) >= 1, str(ann))
    check("公告含旧名->新名", len(ann) >= 1 and "老名字" in ann[0] and "新名字" in ann[0], str(ann))
    mrow = await member_row(MEMBER_ID)
    check("档案已更新为新名", mrow and mrow["first_name"] == "新名字", str(mrow))
    # 再发一条同名消息, 不应重复公告
    bot.calls.clear()
    await feed("又是我", member_user(first_name="新名字", username="xiaoming"))
    check("同名再发不重复公告", len(sent_texts(bot, "改名监控")) == 0)

    # ---------- T7b 改名监控 guard: message_count=0 且无旧名才跳过 ----------
    print("T7b [Bug5 guard] message_count=0 + 有旧名 -> 仍监控; 无旧名 -> 跳过")
    await clear_group_config()
    await seed_rename_watch(enabled=1)
    await seed_member_row(MEMBER_ID, "老名字", "xiaoming", 0)
    bot.calls.clear()
    await feed("大家好", member_user(first_name="新名字", username="xiaoming"))
    check("message_count=0 但有旧名 -> 仍公告", len(sent_texts(bot, "改名监控")) >= 1)
    await clear_group_config()
    await seed_rename_watch(enabled=1)
    await seed_member_row(MEMBER_ID, "", "", 0)
    bot.calls.clear()
    await feed("大家好", member_user(first_name="新名字", username="xiaoming"))
    check("message_count=0 且无旧名 -> 不公告 (guard)", len(sent_texts(bot, "改名监控")) == 0)

    # ---------- T8 定时关群循环不覆写话术锁 ----------
    print("T8 定时循环不覆写未到期的关键词锁")
    await clear_group_config()
    future = (datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=30)).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO scheduled_lockdowns (group_id, start_time, end_time, enabled) VALUES (?, '00:00', '23:59', 1)",
            (GROUP_ID,),
        )
        await db.execute(
            "INSERT INTO group_lock_state (group_id, locked, source, locked_until) VALUES (?, 1, 'keyword', ?)",
            (GROUP_ID, future),
        )
    bot.calls.clear()
    await _apply_scheduled_lockdowns(bot)
    st = await lock_state()
    check("关键词锁未被定时循环覆写", st and st["locked"] == 1 and st["source"] == "keyword", str(st))
    check("locked_until 未变", st and st["locked_until"] == future, str(st))

    # ---------- T9 allow_admin_trigger 列存在 (init_db 迁移) ----------
    print("T9 allow_admin_trigger 列存在")
    await clear_group_config()
    await seed_lockdown_keyword("测试词", allow_admin_trigger=1)
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT allow_admin_trigger FROM lockdown_keywords WHERE group_id = ? AND keyword = ?",
            (GROUP_ID, "测试词"),
        )
    check("allow_admin_trigger=1 能写入读回", row and int(row["allow_admin_trigger"]) == 1, str(row))

    print(f"\n[{MODE}] 通过 {len(passed)} 项, 失败 {len(failed)} 项: {failed}")
    await bot.session.close()
    await close_db()
    sys.exit(1 if failed else 0)


try:
    asyncio.run(main())
finally:
    # 防止中途断言/异常导致连接池线程挂住进程退出
    try:
        asyncio.run(close_db())
    except Exception:
        pass
