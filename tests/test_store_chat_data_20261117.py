"""v2.2 "储存群数据"开关做真测试（2026-11-17）。
覆盖：
  1. group_settings 新列 store_chat_data（新库建表 + 旧库迁移补列）；
  2. log_group_message 受开关控制：关（默认）不写库、开才写入、
     查不到群行不写且不报错；成员计数不受影响不在此测；
  3. POST /api/admin/groups/{id}/store-chat-data：无 token 401、错 token 401、
     未配 token 503、开开关、关开关删存档并返回条数、群不存在 404；
  4. GET /api/admin/groups/{id}/messages：倒序、分页、群不存在 404、无 token 401；
  5. GET /api/admin/groups 返回 store_chat_data 字段且随开关变化。
运行：~/workspace/.venv-bot-test/bin/python -u tests/test_store_chat_data_20261117.py
"""
import os
import sys
import tempfile

os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"
TMP = tempfile.mkdtemp(prefix="store-chat-data-test-")
os.environ["DATABASE_PATH"] = os.path.join(TMP, "bot.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram.types import User

import app.bot.handlers.group_features  # 先加载: 与 main.py 的导入顺序一致, 避免循环 import
from app.bot.handlers.group_features import log_group_message
from app.config import settings
from app.database.db import _migrate_sqlite_columns, close_db, get_db, init_db

GROUP_ID = -100555444333
MISSING_ID = -100000000001
ALICE = User(id=111111, is_bot=False, first_name="alice", username="alice")
ADMIN_TOKEN = "test-admin-token-123"
HDR = {"X-Admin-Token": ADMIN_TOKEN}

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


async def msg_count(gid=GROUP_ID):
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM group_messages WHERE group_id = ?",
            (gid,),
        )
    return row["cnt"] if row else 0


async def group_flag(gid=GROUP_ID):
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT store_chat_data FROM group_settings WHERE group_id = ?",
            (gid,),
        )
    return None if row is None else bool(row.get("store_chat_data"))


async def main():
    await init_db()
    try:
        # 1. 新库建表自带 store_chat_data 列
        import aiosqlite

        async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as conn:
            cur = await conn.execute("PRAGMA table_info(group_settings)")
            cols = {row[1] for row in await cur.fetchall()}
        check("新库有 store_chat_data 列", "store_chat_data" in cols)

        # 1b. 旧库迁移补列
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
            cur = await conn.execute(
                "SELECT store_chat_data FROM group_settings WHERE group_id = 1"
            )
            old_val = (await cur.fetchone())[0]
        check("旧库迁移补 store_chat_data", "store_chat_data" in old_cols)
        check("旧库已有行默认 0", old_val == 0, str(old_val))

        # 2. 建群行（默认关）→ log_group_message 不写库
        async with get_db() as db:
            await db.execute(
                "INSERT INTO group_settings (group_id, group_title, is_active) "
                "VALUES (?, ?, 1)",
                (GROUP_ID, "储存开关测试群"),
            )
        check("新群开关默认关", await group_flag() is False)
        await log_group_message(GROUP_ID, 101, ALICE, "关着时不该存")
        check("开关关时不归档", await msg_count() == 0)

        # 查不到群行：不写、不报错
        await log_group_message(MISSING_ID, 1, ALICE, "无群行")
        check("无群行不归档不报错", await msg_count(MISSING_ID) == 0)

        # 3. 直接开开关（模拟已开启状态）→ 写入 3 条
        async with get_db() as db:
            await db.execute(
                "UPDATE group_settings SET store_chat_data = 1 WHERE group_id = ?",
                (GROUP_ID,),
            )
        for i, txt in enumerate(("第一条", "第二条", "第三条")):
            await log_group_message(GROUP_ID, 201 + i, ALICE, txt)
        check("开关开时归档 3 条", await msg_count() == 3)

        # 4~5. 接口（TestClient 只开一次, 同 v2.1 测试的注意事项）
        from fastapi.testclient import TestClient
        from app.main import app

        settings.ADMIN_SYNC_TOKEN = ADMIN_TOKEN
        base = f"/api/admin/groups/{GROUP_ID}"
        with TestClient(app) as client:
            r = client.post(f"{base}/store-chat-data", json={"enabled": True})
            check("POST 无 token 401", r.status_code == 401)
            r = client.post(f"{base}/store-chat-data", json={"enabled": True},
                            headers={"X-Admin-Token": "wrong"})
            check("POST 错 token 401", r.status_code == 401)
            r = client.get(f"{base}/messages")
            check("GET messages 无 token 401", r.status_code == 401)

            # GET groups 带字段且为开
            r = client.get("/api/admin/groups", headers=HDR)
            check("GET groups 200", r.status_code == 200)
            g = next(x for x in r.json()["groups"] if x["group_id"] == GROUP_ID)
            check("GET groups 带 store_chat_data=true", g.get("store_chat_data") is True, str(g))

            # GET messages 倒序 + total
            r = client.get(f"{base}/messages", headers=HDR)
            check("GET messages 200", r.status_code == 200)
            data = r.json()
            check("messages total=3", data.get("total") == 3, str(data))
            texts = [m["text"] for m in data.get("messages", [])]
            check("messages 按最新在前倒序",
                  texts == ["第三条", "第二条", "第一条"], str(texts))
            m0 = data["messages"][0]
            check("message 字段齐全",
                  all(k in m0 for k in ("id", "message_id", "user_id",
                                        "username", "text", "created_at")), str(m0))
            check("message user 正确",
                  m0["user_id"] == ALICE.id and m0["username"] == "alice", str(m0))

            # 分页: limit / offset
            r = client.get(f"{base}/messages?limit=2", headers=HDR)
            check("limit=2 返回 2 条", len(r.json()["messages"]) == 2)
            r = client.get(f"{base}/messages?limit=2&offset=2", headers=HDR)
            tail = [m["text"] for m in r.json()["messages"]]
            check("offset=2 剩最旧 1 条", tail == ["第一条"], str(tail))
            r = client.get(f"{base}/messages?limit=0", headers=HDR)
            check("limit=0 夹到 1 条", len(r.json()["messages"]) == 1)

            # POST 开开关（已开）: deleted_messages=0, 不删档
            r = client.post(f"{base}/store-chat-data", json={"enabled": True}, headers=HDR)
            body = r.json()
            check("POST 开开关 200", r.status_code == 200)
            check("POST 开开关回字段",
                  body.get("status") == "ok" and body.get("group_id") == GROUP_ID
                  and body.get("store_chat_data") is True
                  and body.get("deleted_messages") == 0, str(body))
            check("开开关不删档", await msg_count() == 3)

            # POST 关开关: 删光存档并返回条数
            r = client.post(f"{base}/store-chat-data", json={"enabled": False}, headers=HDR)
            body = r.json()
            check("POST 关开关回 deleted=3",
                  body.get("store_chat_data") is False
                  and body.get("deleted_messages") == 3, str(body))
            check("关开关后存档清空", await msg_count() == 0)
            check("关开关后库里标记为关", await group_flag() is False)
            r = client.get("/api/admin/groups", headers=HDR)
            g = next(x for x in r.json()["groups"] if x["group_id"] == GROUP_ID)
            check("GET groups 反映已关", g.get("store_chat_data") is False, str(g))

            # 关后再调 log_group_message 也不写（端到端）
            await log_group_message(GROUP_ID, 301, ALICE, "关了之后")
            check("接口关开关后归档停写", await msg_count() == 0)

            # 群不存在 404
            r = client.post(f"/api/admin/groups/{MISSING_ID}/store-chat-data",
                            json={"enabled": True}, headers=HDR)
            check("POST 群不存在 404", r.status_code == 404)
            r = client.get(f"/api/admin/groups/{MISSING_ID}/messages", headers=HDR)
            check("GET messages 群不存在 404", r.status_code == 404)

            # 未配 token 503
            settings.ADMIN_SYNC_TOKEN = ""
            r = client.post(f"{base}/store-chat-data", json={"enabled": True},
                            headers={"X-Admin-Token": "x"})
            check("POST 未配 token 503", r.status_code == 503)
            r = client.get(f"{base}/messages", headers={"X-Admin-Token": "x"})
            check("GET messages 未配 token 503", r.status_code == 503)
            settings.ADMIN_SYNC_TOKEN = ADMIN_TOKEN
    finally:
        await close_db()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    print("全部通过" if failed == 0 else "有失败项")
    return failed == 0


if __name__ == "__main__":
    import asyncio

    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
