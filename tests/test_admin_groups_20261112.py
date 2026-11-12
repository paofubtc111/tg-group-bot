"""v2.1 群组登记修复测试（2026-11-12）。
覆盖：
  1. group_settings 新列 added_by_user_id / added_by_username（新库建表 + 旧库迁移补列）；
  2. my_chat_member 拉群时记下操作人；提权不覆盖；踢出后重拉更新为新操作人；
     机器人自己操作时不记；
  3. GET /api/admin/groups 鉴权（无 token 401、错 token 401、未配 token 503、
     正确 token 200 返回全表字段）。
运行：NO_PROXY=... ~/workspace/.venv-bot-test/bin/python -u tests/test_admin_groups_20261112.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"
TMP = tempfile.mkdtemp(prefix="admin-groups-test-")
os.environ["DATABASE_PATH"] = os.path.join(TMP, "bot.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    User,
)

import app.bot.handlers.group_features  # 先加载: 与 main.py 的导入顺序一致, 避免循环 import
from app.bot.middlewares.group_tracker import on_bot_member_updated
from app.config import settings
from app.database.db import _migrate_sqlite_columns, close_db, get_db, init_db

GROUP_ID = -100999888777
GROUP_TITLE = "测试群"
ALICE_ID = 111111
BOB_ID = 222222
OTHER_BOT_ID = 999999

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {name} {detail}")
    else:
        failed += 1
        print(f"FAIL {name} {detail}")


def group_chat():
    return Chat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)


def user(uid, username):
    return User(id=uid, is_bot=False, first_name=username, username=username)


def bot_user():
    return User(id=OTHER_BOT_ID, is_bot=True, first_name="otherbot", username="otherbot")


def member_event(actor, old_member, new_member):
    return ChatMemberUpdated(
        chat=group_chat(),
        from_user=actor,
        date=datetime.now(timezone.utc),
        old_chat_member=old_member,
        new_chat_member=new_member,
    )


def admin_member():
    return ChatMemberAdministrator(
        status="administrator",
        user=User(id=9257427138, is_bot=True, first_name="test2", username="flowerGPbot"),
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


def bot_member():
    return User(id=9257427138, is_bot=True, first_name="test2", username="flowerGPbot")


async def group_row():
    async with get_db() as db:
        return await db.fetchrow(
            "SELECT group_id, group_title, is_active, added_by_user_id, "
            "added_by_username FROM group_settings WHERE group_id = ?",
            (GROUP_ID,),
        )


async def main():
    global passed, failed
    await init_db()
    try:
        # 1. 新库建表自带两列
        import aiosqlite

        async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as conn:
            cur = await conn.execute(f"PRAGMA table_info(group_settings)")
            cols = {row[1] for row in await cur.fetchall()}
        check("新库有 added_by_user_id 列", "added_by_user_id" in cols)
        check("新库有 added_by_username 列", "added_by_username" in cols)

        # 1b. 旧库（无两列）跑迁移能补列（迁移会遍历所有 _NEW_COLUMNS 的表，
        # 所以旧库里这些表都要先建好最小结构；生产上 init_db 先跑全量 schema）
        old_db = os.path.join(TMP, "old.db")
        async with aiosqlite.connect(old_db) as conn:
            for tbl in ("lockdown_keywords", "group_moderation_settings",
                        "ai_news_items", "ai_news_settings"):
                await conn.execute(f"CREATE TABLE {tbl} (id INTEGER PRIMARY KEY)")
            await conn.execute(
                "CREATE TABLE group_settings (group_id INTEGER PRIMARY KEY, "
                "group_title TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            await conn.execute(
                "INSERT INTO group_settings (group_id, group_title) VALUES (1, '旧群')"
            )
            await conn.commit()
            await _migrate_sqlite_columns(conn)
            cur = await conn.execute("PRAGMA table_info(group_settings)")
            old_cols = {row[1] for row in await cur.fetchall()}
        check("旧库迁移补 added_by_user_id", "added_by_user_id" in old_cols)
        check("旧库迁移补 added_by_username", "added_by_username" in old_cols)

        # 2. 首次拉群：Alice 拉入，记下操作人
        await on_bot_member_updated(member_event(
            user(ALICE_ID, "alice"),
            ChatMemberLeft(status="left", user=bot_member()),
            ChatMemberMember(status="member", user=bot_member()),
        ))
        row = await group_row()
        check("拉群后群已登记", row is not None, str(row))
        check("拉群记下操作人 id", row and row["added_by_user_id"] == ALICE_ID, str(row))
        check("拉群记下操作人用户名", row and row["added_by_username"] == "alice", str(row))
        check("拉群后 is_active=1", row and row["is_active"] == 1, str(row))

        # 3. 提权为管理员（Bob 操作）：不覆盖操作人
        await on_bot_member_updated(member_event(
            user(BOB_ID, "bob"),
            ChatMemberMember(status="member", user=bot_member()),
            admin_member(),
        ))
        row = await group_row()
        check("提权不覆盖操作人 id", row and row["added_by_user_id"] == ALICE_ID, str(row))
        check("提权不覆盖操作人用户名", row and row["added_by_username"] == "alice", str(row))
        check("提权后仍 active", row and row["is_active"] == 1, str(row))

        # 4. 踢出：is_active=0，操作人保留
        await on_bot_member_updated(member_event(
            user(BOB_ID, "bob"),
            admin_member(),
            ChatMemberLeft(status="left", user=bot_member()),
        ))
        row = await group_row()
        check("踢出后 is_active=0", row and row["is_active"] == 0, str(row))
        check("踢出后操作人保留", row and row["added_by_user_id"] == ALICE_ID, str(row))

        # 5. 被踢后由 Bob 重新拉入：操作人更新为 Bob
        await on_bot_member_updated(member_event(
            user(BOB_ID, "bob"),
            ChatMemberLeft(status="left", user=bot_member()),
            ChatMemberMember(status="member", user=bot_member()),
        ))
        row = await group_row()
        check("重拉后 is_active=1", row and row["is_active"] == 1, str(row))
        check("重拉更新操作人 id", row and row["added_by_user_id"] == BOB_ID, str(row))
        check("重拉更新操作人用户名", row and row["added_by_username"] == "bob", str(row))

        # 6. 机器人自己操作（先踢出再由机器人拉入）：不记机器人为操作人
        await on_bot_member_updated(member_event(
            user(BOB_ID, "bob"),
            ChatMemberMember(status="member", user=bot_member()),
            ChatMemberLeft(status="left", user=bot_member()),
        ))
        await on_bot_member_updated(member_event(
            bot_user(),
            ChatMemberLeft(status="left", user=bot_member()),
            ChatMemberMember(status="member", user=bot_member()),
        ))
        row = await group_row()
        check("机器人操作不覆盖操作人", row and row["added_by_user_id"] == BOB_ID, str(row))

        # 7. 接口鉴权 + 返回字段（TestClient）
        from fastapi.testclient import TestClient
        from app.main import app

        settings.ADMIN_SYNC_TOKEN = "test-admin-token-123"
        with TestClient(app) as client:
            r = client.get("/api/admin/groups")
            check("无 token 401", r.status_code == 401)
            r = client.get("/api/admin/groups", headers={"X-Admin-Token": "wrong"})
            check("错 token 401", r.status_code == 401)
            r = client.get("/api/admin/groups", headers={"X-Admin-Token": "test-admin-token-123"})
            check("正确 token 200", r.status_code == 200)
            data = r.json()
            check("返回 status ok", data.get("status") == "ok")
            groups = data.get("groups", [])
            check("列表含测试群", any(g["group_id"] == GROUP_ID for g in groups), str(groups))
            g = next(x for x in groups if x["group_id"] == GROUP_ID)
            check("返回群名", g["group_title"] == GROUP_TITLE, str(g))
            check("返回 is_active", g["is_active"] is True, str(g))
            check("返回操作人 id", g["added_by_user_id"] == BOB_ID, str(g))
            check("返回操作人用户名", g["added_by_username"] == "bob", str(g))
            check("返回 created_at", "created_at" in g, str(g))
            check("返回 updated_at", "updated_at" in g, str(g))
            # 未配 token 503：同一 TestClient 内改设置即可（lifespan 只跑一次，
            # 第二次进 TestClient 会因 router 单例已挂载而报错，故不另开 client）
            settings.ADMIN_SYNC_TOKEN = ""
            r = client.get("/api/admin/groups", headers={"X-Admin-Token": "x"})
            check("未配 token 503", r.status_code == 503)
            settings.ADMIN_SYNC_TOKEN = "test-admin-token-123"
    finally:
        await close_db()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    print("全部通过" if failed == 0 else "有失败项")
    return failed == 0


if __name__ == "__main__":
    import asyncio

    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
