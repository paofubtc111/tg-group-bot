# -*- coding: utf-8 -*-
"""群消息导出 Excel 测试。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \\
    ~/workspace/.venv-bot-test/bin/python tests/test_messages_export_20261127.py

覆盖（pao 2026-11-27 21:09 要的功能）：
- GET /api/group/{gid}/messages/export 返回 .xlsx，二进制内容能用 openpyxl 读回；
- 表头：时间（北京时间）/ 消息 ID / 用户 ID / 用户名 / 消息内容；
- 行按时间从旧到新（id 升序），UTC 存档时间转北京时间（+8）；
- ?q= 搜索过滤只导出筛出来的行；搜不到时只有表头；
- 超长内容截断到 32000 字以内。
"""
import io
import os
import sys

os.environ["DATABASE_PATH"] = "/tmp/test_messages_export.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.auth import get_current_user
from app.database.db import close_db, ensure_group_initialized, get_db, init_db

GROUP = -1004496842671
ADMIN = 8449446199


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


async def main():
    await close_db()
    if os.path.exists("/tmp/test_messages_export.db"):
        os.remove("/tmp/test_messages_export.db")
    await init_db()
    await ensure_group_initialized(GROUP)
    await ensure_group_initialized(GROUP + 1)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_messages (group_id, message_id, user_id, username, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GROUP, 101, 111, "alice", "大家好", "2026-11-27 10:00:00"),
        )
        await db.execute(
            "INSERT INTO group_messages (group_id, message_id, user_id, username, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GROUP, 102, 222, "bob", "AI 新闻来了", "2026-11-27 11:30:00"),
        )
        await db.execute(
            "INSERT INTO group_messages (group_id, message_id, user_id, username, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GROUP, 103, 111, "alice", "x" * 40000, "2026-11-27 12:00:00"),
        )
        # 别群的消息不能混进来
        await db.execute(
            "INSERT INTO group_messages (group_id, message_id, user_id, username, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GROUP + 1, 201, 333, "mallory", "别群的", "2026-11-27 10:00:00"),
        )

    from app.main import app
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": ADMIN, "first_name": "T", "username": "t"}
    ok = True

    resp = client.get(f"/api/group/{GROUP}/messages/export")
    ok &= check("导出 200", resp.status_code == 200)
    ok &= check("Content-Type 是 xlsx",
                "spreadsheetml" in resp.headers.get("content-type", ""))
    ok &= check("Content-Disposition 带 .xlsx 文件名",
                ".xlsx" in resp.headers.get("content-disposition", ""))
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb["群消息"]
    rows = list(ws.iter_rows(values_only=True))
    ok &= check("表头 5 列",
                rows[0] == ("时间（北京时间）", "消息 ID", "用户 ID", "用户名", "消息内容"))
    ok &= check("共 3 行数据（别群的不在内）", len(rows) == 4)
    ok &= check("行按时间从旧到新",
                [r[1] for r in rows[1:]] == [101, 102, 103])
    ok &= check("UTC 10:00 转北京时间 18:00",
                str(rows[1][0]) == "2026-11-27 18:00:00")
    ok &= check("用户名与内容原样",
                rows[2][3] == "bob" and rows[2][4] == "AI 新闻来了")
    ok &= check("超长内容截断到 32000 字",
                len(rows[3][4]) == 32000)

    resp_q = client.get(f"/api/group/{GROUP}/messages/export?q=AI")
    wb_q = load_workbook(io.BytesIO(resp_q.content))
    rows_q = list(wb_q["群消息"].iter_rows(values_only=True))
    ok &= check("?q=AI 只导出 1 行", len(rows_q) == 2 and rows_q[1][1] == 102)

    resp_none = client.get(f"/api/group/{GROUP}/messages/export?q=不存在的词")
    wb_n = load_workbook(io.BytesIO(resp_none.content))
    rows_n = list(wb_n["群消息"].iter_rows(values_only=True))
    ok &= check("搜不到时只有表头", len(rows_n) == 1)

    print("ALL PASS" if ok else "SOME FAILED")
    await close_db()
    return ok


if __name__ == "__main__":
    import asyncio

    raise SystemExit(0 if asyncio.run(main()) else 1)
