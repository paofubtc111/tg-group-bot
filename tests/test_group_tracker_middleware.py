"""回归测试: GroupTrackerMiddleware 在 dp.update.outer_middleware 下的接线。

用真实 Dispatcher + 假 Bot(不发任何网络请求)+ 临时数据库, 验证 Update
包着内层事件时中间件能正常登记群组、补记成员、翻回 is_active。

用法:
  TEST_DB_MODE=sqlite /tmp/pgtest/bin/python /tmp/pgtest/test_middleware.py
  TEST_DB_MODE=pg     /tmp/pgtest/bin/python /tmp/pgtest/test_middleware.py
  TEST_REPO=<另一份代码路径>  可指定要测的代码树 (用于旧代码对照)
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

MODE = os.environ.get("TEST_DB_MODE", "sqlite")
REPO = os.environ.get(
    "TEST_REPO", "/home/hatch/workspace/tg-render-deploy/tg-group-bot"
)
sys.path.insert(0, REPO)

# ---- 环境变量必须在 import app 之前定好 ----
if MODE == "pg":
    import pgserver

    _server = pgserver.get_server("/tmp/pgtest-pgdata-mw")
    os.environ["DATABASE_URL"] = _server.get_uri()
    os.environ.pop("DATABASE_PATH", None)
else:
    os.environ.pop("DATABASE_URL", None)
    _db_path = "/tmp/pgtest/mw_test_bot.db"
    if os.path.exists(_db_path):
        os.remove(_db_path)
    os.environ["DATABASE_PATH"] = _db_path

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)

import app.bot.handlers.group_features  # 先加载: 与 main.py 的导入顺序一致, 避免循环 import
from app.bot.middlewares.group_tracker import GroupTrackerMiddleware, tracker_router
from app.database.db import close_db, ensure_group_initialized, get_db, init_db

GROUP_ID = -1004496842671
GROUP_TITLE = "拥抱A i"
USER_ID = 8449446199
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


def group_chat():
    return Chat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)


def group_user():
    return User(id=USER_ID, is_bot=False, first_name="泡芙", username="yujinxiang_u")


def bot_user():
    return User(id=BOT_ID, is_bot=True, first_name="test2", username="flowerGPbot")


def group_message(message_id, text):
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=group_chat(),
        from_user=group_user(),
        text=text,
    )


async def group_row():
    async with get_db() as db:
        return await db.fetchrow(
            "SELECT group_id, group_title, is_active FROM group_settings WHERE group_id = ?",
            (GROUP_ID,),
        )


async def member_row():
    async with get_db() as db:
        return await db.fetchrow(
            "SELECT group_id, user_id, message_count FROM group_members WHERE group_id = ? AND user_id = ?",
            (GROUP_ID, USER_ID),
        )


async def main():
    await init_db()
    print(f"[{MODE}] init_db ok")

    calls = []
    test_router = Router(name="test_router")

    @test_router.message()
    async def catch_all_message(message: Message):
        calls.append(("message", message.text))

    @test_router.edited_message()
    async def catch_all_edited(message: Message):
        calls.append(("edited_message", message.text))

    @test_router.callback_query()
    async def catch_all_callback(query: CallbackQuery):
        calls.append(("callback", query.data))

    dp = Dispatcher()
    dp.update.outer_middleware(GroupTrackerMiddleware())
    dp.include_router(tracker_router)
    dp.include_router(test_router)
    bot = Bot(token="123456789:" + "A" * 35)

    # ---- 0. 对照: Update 本身不是 Message, 旧代码的 isinstance 永远对不上 ----
    upd0 = Update(update_id=1, message=group_message(1, "/start"))
    check("Update 不是 Message (旧分支必然落空)", not isinstance(upd0, Message))

    # ---- T1: 群 /start 的 Update 走 Dispatcher, 群要登记上 ----
    print("T1 群 /start Update -> 登记 + 成员补记 + handler 链")
    await dp.feed_update(bot, Update(update_id=100, message=group_message(10, "/start")))
    check("handler 链跑通 (/start 到达测试 handler)", ("message", "/start") in calls)
    row = await group_row()
    check("group_settings 出现该群", row is not None, str(row))
    check("标题为真实群名", row and row["group_title"] == GROUP_TITLE, str(row))
    check("is_active=1", row and row["is_active"] == 1, str(row))
    mrow = await member_row()
    check("track_member_seen 生效 (group_members 有该成员)", mrow is not None, str(mrow))
    check("补记不加消息数", mrow and mrow["message_count"] == 0, str(mrow))
    async with get_db() as db:
        n = await db.fetchrow("SELECT COUNT(*) AS c FROM group_settings")
    check("群只登记一次, 无重复行", n and n["c"] == 1, str(n))

    # ---- T2: 预置 is_active=0, 再喂一条群消息, 应翻回 1 ----
    print("T2 is_active=0 预置 -> 再喂群消息 -> 翻回 1")
    async with get_db() as db:
        await db.execute(
            "UPDATE group_settings SET is_active = 0 WHERE group_id = ?", (GROUP_ID,)
        )
    row = await group_row()
    check("预置成功 is_active=0", row and row["is_active"] == 0, str(row))
    await dp.feed_update(bot, Update(update_id=101, message=group_message(11, "hello")))
    row = await group_row()
    check("喂消息后 is_active 翻回 1", row and row["is_active"] == 1, str(row))
    check("标题未被覆盖", row and row["group_title"] == GROUP_TITLE, str(row))

    # ---- T3: ensure_group_initialized 默认标题不抹真实群名 (走 else 分支) ----
    print("T3 默认标题调用不抹真实群名")
    await ensure_group_initialized(GROUP_ID)
    row = await group_row()
    check("标题仍为真实群名", row and row["group_title"] == GROUP_TITLE, str(row))
    check("is_active 仍为 1", row and row["is_active"] == 1, str(row))

    # ---- T4: CallbackQuery 的 Update 也要拆开 ----
    print("T4 群内 CallbackQuery Update -> 拆包 + 成员补记")
    cq = CallbackQuery(
        id="cq1",
        from_user=group_user(),
        chat_instance="ci1",
        message=group_message(12, "按钮消息"),
        data="btn:test",
    )
    await dp.feed_update(bot, Update(update_id=102, callback_query=cq))
    check("callback 到达测试 handler", ("callback", "btn:test") in calls)
    row = await group_row()
    check("callback 后群仍在且 is_active=1", row and row["is_active"] == 1, str(row))

    # ---- T5: my_chat_member 拉入/踢出线仍正常 ----
    print("T5 my_chat_member: 拉入当管理员 -> is_active=1; 踢出 -> is_active=0")

    def my_chat_member(update_id, new_member):
        return Update(
            update_id=update_id,
            my_chat_member=ChatMemberUpdated(
                chat=group_chat(),
                from_user=group_user(),
                date=datetime.now(timezone.utc),
                old_chat_member=ChatMemberMember(status="member", user=bot_user()),
                new_chat_member=new_member,
            ),
        )

    admin = ChatMemberAdministrator(
        status="administrator",
        user=bot_user(),
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
    await dp.feed_update(bot, my_chat_member(103, admin))
    row = await group_row()
    check("拉入当管理员后 is_active=1", row and row["is_active"] == 1, str(row))
    await dp.feed_update(
        bot, my_chat_member(104, ChatMemberLeft(status="left", user=bot_user()))
    )
    row = await group_row()
    check("踢出后 is_active=0 (handler 的显式置 0 未被中间件翻回盖掉)", row and row["is_active"] == 0, str(row))

    # ---- T6: edited_message 拆包不报错, 且能把群重新激活 ----
    print("T6 edited_message Update -> 拆包正常, 群重新激活")
    await dp.feed_update(
        bot,
        Update(update_id=105, edited_message=group_message(11, "hello edited")),
    )
    row = await group_row()
    check("edited_message 后 is_active 回到 1", row and row["is_active"] == 1, str(row))
    check("handler 链对 edited_message 也跑通", ("edited_message", "hello edited") in calls)

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
