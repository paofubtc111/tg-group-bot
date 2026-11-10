"""回归测试: 改名定时巡查 (rename_watch_loop / _check_renames_once)。

用假 Bot(get_chat_member 返回可控的用户和状态)+ 真实数据库, 验证机器人
每 30 分钟主动巡查改名监控已开启的群, 不用等成员发言：

  (a) 成员改了昵称 -> 公告且档案更新成新名
  (b) 成员改了用户名 -> 公告
  (c) 成员没改名 -> 不公告
  (d) 成员已退群 (left) -> 不公告
  (e) 改名监控开关关着 -> 不公告
  (f) 成员不在 group_members 里 -> 不公告
  (g) 公告并更新档案后第二轮巡查 -> 不重复公告
另带: 限流时本轮停止不 crash；循环本身能启动多轮、能正常取消。

用法:
  TEST_DB_MODE=sqlite /tmp/pgtest/bin/python tests/test_rename_watch_loop.py
  TEST_DB_MODE=pg     /tmp/pgtest/bin/python tests/test_rename_watch_loop.py
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

    _server = pgserver.get_server("/tmp/pgtest-pgdata-rename")
    os.environ["DATABASE_URL"] = _server.get_uri()
    os.environ.pop("DATABASE_PATH", None)
else:
    os.environ.pop("DATABASE_URL", None)
    _db_path = "/tmp/pgtest/rename_watch_test_bot.db"
    if os.path.exists(_db_path):
        os.remove(_db_path)
    os.environ["DATABASE_PATH"] = _db_path

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetChatMember
from aiogram.types import (
    Chat,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    User,
)

import app.bot.handlers.group_features  # 先加载: 与 main.py 的导入顺序一致, 避免循环 import
from app.bot.handlers.group_features import _check_renames_once, rename_watch_loop
from app.database.db import close_db, ensure_group_initialized, get_db, init_db

GROUP_ID = -1004496842671
GROUP_TITLE = "拥抱A i"
MEMBER_ID = 777001
MEMBER2_ID = 777002
MEMBER3_ID = 777003
GHOST_ID = 777099
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
    """不发网络请求的 Bot: get_chat_member 按 user_id 返回预设的用户和状态。"""

    def __init__(self):
        super().__init__(token="123456789:" + "A" * 35)
        self.calls = []
        self._msg_seq = 2000
        self.profiles = {}
        self.rate_limit_uids = set()

    def set_profile(self, user_id, first_name, username, status="member"):
        user = User(id=user_id, is_bot=False, first_name=first_name, username=username)
        self.profiles[user_id] = (user, status)

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(("get_chat_member", chat_id, user_id))
        if user_id in self.rate_limit_uids:
            raise TelegramRetryAfter(
                GetChatMember(chat_id=chat_id, user_id=user_id),
                "Too Many Requests: retry after 5",
                5,
            )
        user, status = self.profiles.get(
            user_id,
            (User(id=user_id, is_bot=False, first_name="M", username="m"), "member"),
        )
        if status == "left":
            return ChatMemberLeft(status="left", user=user)
        if status == "kicked":
            return ChatMemberBanned(status="kicked", user=user, until_date=0)
        return ChatMemberMember(status="member", user=user)

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


def sent_texts(bot, substr):
    return [c[2] for c in bot.calls if c[0] == "send_message" and substr in c[2]]


def checked_uids(bot):
    return [c[2] for c in bot.calls if c[0] == "get_chat_member"]


async def seed_watch(enabled=1):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_rename_watch (group_id, enabled) VALUES (?, ?) ON CONFLICT(group_id) DO UPDATE SET enabled = excluded.enabled",
            (GROUP_ID, enabled),
        )


async def seed_member(uid, first_name, username, message_count=5):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_members (group_id, user_id, username, first_name, message_count, last_seen, joined_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT(group_id, user_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name, message_count = excluded.message_count",
            (GROUP_ID, uid, username, first_name, message_count),
        )


async def member_row(uid):
    async with get_db() as db:
        return await db.fetchrow(
            "SELECT username, first_name, message_count FROM group_members WHERE group_id = ? AND user_id = ?",
            (GROUP_ID, uid),
        )


async def clear_all():
    async with get_db() as db:
        await db.execute("DELETE FROM group_members WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM group_rename_watch WHERE group_id = ?", (GROUP_ID,))


async def main():
    await init_db()
    print(f"[{MODE}] init_db ok")
    await ensure_group_initialized(GROUP_ID, GROUP_TITLE)
    bot = FakeBot()

    # ---------- T1 (a) 改了昵称 -> 公告且档案更新 ----------
    print("T1 (a) 改了昵称 -> 公告且档案更新")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "老名字", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "新名字", "xiaoming")
    bot.calls.clear()
    await _check_renames_once(bot)
    ann = sent_texts(bot, "改名监控")
    check("改昵称发了公告", len(ann) == 1, str(ann))
    check("公告含旧名->新名", ann and "老名字" in ann[0] and "新名字" in ann[0], str(ann))
    row = await member_row(MEMBER_ID)
    check("档案已更新成新名", row and row["first_name"] == "新名字", str(row))

    # ---------- T2 (b) 改了用户名 -> 公告 ----------
    print("T2 (b) 改了用户名 -> 公告")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "小明", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "小明", "xiaoming_new")
    bot.calls.clear()
    await _check_renames_once(bot)
    ann = sent_texts(bot, "改名监控")
    check("改用户名发了公告", len(ann) == 1, str(ann))
    check("公告含旧用户名->新用户名", ann and "xiaoming" in ann[0] and "xiaoming_new" in ann[0], str(ann))
    row = await member_row(MEMBER_ID)
    check("档案用户名已更新", row and row["username"] == "xiaoming_new", str(row))

    # ---------- T3 (c) 没改名 -> 不公告 ----------
    print("T3 (c) 没改名 -> 不公告")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "小明", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "小明", "xiaoming")
    bot.calls.clear()
    await _check_renames_once(bot)
    check("没改名不公告", len(sent_texts(bot, "改名监控")) == 0)
    check("没改名也查了该成员", MEMBER_ID in checked_uids(bot))

    # ---------- T4 (d) 已退群 -> 不公告 ----------
    print("T4 (d) 已退群 -> 不公告")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "老名字", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "新名字", "xiaoming", status="left")
    bot.calls.clear()
    await _check_renames_once(bot)
    check("已退群不公告", len(sent_texts(bot, "改名监控")) == 0)
    row = await member_row(MEMBER_ID)
    check("已退群不更新档案", row and row["first_name"] == "老名字", str(row))

    # ---------- T5 (e) 监控开关关着 -> 不公告 ----------
    print("T5 (e) 监控开关关着 -> 不公告")
    await clear_all()
    await seed_watch(0)
    await seed_member(MEMBER_ID, "老名字", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "新名字", "xiaoming")
    bot.calls.clear()
    await _check_renames_once(bot)
    check("开关关着不公告", len(sent_texts(bot, "改名监控")) == 0)
    check("开关关着不查成员", MEMBER_ID not in checked_uids(bot))

    # ---------- T6 (f) 成员不在 group_members 里 -> 不公告 ----------
    print("T6 (f) 成员不在 group_members 里 -> 不公告")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "小明", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "小明", "xiaoming")
    bot.set_profile(GHOST_ID, "幽灵新名", "ghost")
    bot.calls.clear()
    await _check_renames_once(bot)
    check("不在档案里的成员不公告", len(sent_texts(bot, "改名监控")) == 0)
    check("不在档案里的成员不查", GHOST_ID not in checked_uids(bot))

    # ---------- T7 (g) 第二轮巡查 -> 不重复公告 ----------
    print("T7 (g) 公告后第二轮巡查 -> 不重复公告")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "老名字", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "新名字", "xiaoming")
    bot.calls.clear()
    await _check_renames_once(bot)
    check("第一轮公告 1 次", len(sent_texts(bot, "改名监控")) == 1)
    bot.calls.clear()
    await _check_renames_once(bot)
    check("第二轮不重复公告", len(sent_texts(bot, "改名监控")) == 0)

    # ---------- T8 限流时本轮停止，不 crash ----------
    print("T8 限流时本轮停止，不 crash")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "老名字", "xiaoming", 5)
    await seed_member(MEMBER2_ID, "老名字2", "xiaoming2", 5)
    bot.set_profile(MEMBER_ID, "新名字", "xiaoming")
    bot.set_profile(MEMBER2_ID, "新名字2", "xiaoming2")
    bot.rate_limit_uids = {MEMBER_ID}
    bot.calls.clear()
    await _check_renames_once(bot)  # 不应抛异常
    check("限流不 crash", True)
    check("限流后本轮停止（后一个成员没被查）", MEMBER2_ID not in checked_uids(bot))
    check("限流后本轮无公告", len(sent_texts(bot, "改名监控")) == 0)
    bot.rate_limit_uids = set()

    # ---------- T9 循环能启动多轮、能正常取消 ----------
    print("T9 循环能启动多轮、能正常取消")
    await clear_all()
    await seed_watch(1)
    await seed_member(MEMBER_ID, "老名字", "xiaoming", 5)
    bot.set_profile(MEMBER_ID, "新名字", "xiaoming")
    bot.calls.clear()
    task = asyncio.create_task(rename_watch_loop(bot, interval_seconds=0.05))
    await asyncio.sleep(0.6)
    task.cancel()
    try:
        await task
        cancelled_cleanly = False
    except asyncio.CancelledError:
        cancelled_cleanly = True
    check("循环取消干净（CancelLedError 正常抛出）", cancelled_cleanly)
    check("循环多轮只公告 1 次（不重复）", len(sent_texts(bot, "改名监控")) == 1)
    row = await member_row(MEMBER_ID)
    check("循环后档案是新名", row and row["first_name"] == "新名字", str(row))

    await close_db()
    print(f"\n[{MODE}] 通过 {len(passed)} 项, 失败 {len(failed)} 项: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
