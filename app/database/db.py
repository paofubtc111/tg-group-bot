import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional
import aiosqlite

from app.config import settings


class AsyncDBSession:
    """
    封装 aiosqlite.Connection，提供类似 asyncpg 的 fetch / fetchrow / execute 异步便捷方法，
    并统一返回字典结构，保证与 PRD 核心代码规范完全兼容。
    """

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def fetch(self, query: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetchrow(self, query: str, params: tuple | list = ()) -> Optional[dict[str, Any]]:
        async with self.conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def execute(self, query: str, params: tuple | list = ()) -> int:
        async with self.conn.execute(query, params) as cursor:
            await self.conn.commit()
            return cursor.lastrowid or cursor.rowcount

    async def executemany(self, query: str, seq_of_params: list[tuple | list]) -> None:
        await self.conn.executemany(query, seq_of_params)
        await self.conn.commit()


class ConnectionPool:
    """
    轻量级 aiosqlite 异步连接池，避免高频并发创建连接开销并保证 WAL 并发读写安全。
    """

    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=pool_size)
        self._initialized = False
        self._lock = asyncio.Lock()

    async def _create_connection(self) -> aiosqlite.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.db_path, timeout=30.0)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA journal_mode = WAL;")
        await conn.execute("PRAGMA synchronous = NORMAL;")
        await conn.commit()
        return conn

    async def init_pool(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            for _ in range(self.pool_size):
                conn = await self._create_connection()
                await self._pool.put(conn)
            self._initialized = True

    async def acquire(self) -> aiosqlite.Connection:
        if not self._initialized:
            await self.init_pool()
        try:
            return self._pool.get_nowait()
        except asyncio.QueueEmpty:
            return await self._create_connection()

    async def release(self, conn: aiosqlite.Connection) -> None:
        try:
            self._pool.put_nowait(conn)
        except asyncio.QueueFull:
            await conn.close()

    async def close_all(self) -> None:
        while not self._pool.empty():
            conn = await self._pool.get()
            await conn.close()
        self._initialized = False


_pool = ConnectionPool(settings.DATABASE_PATH)


@asynccontextmanager
async def get_db() -> AsyncIterator[AsyncDBSession]:
    """
    获取异步数据库会话上下文管理器
    """
    conn = await _pool.acquire()
    try:
        yield AsyncDBSession(conn)
    finally:
        await _pool.release(conn)


async def init_db() -> None:
    """
    初始化数据库 Schema 并确保默认全局欢迎模板存在
    """
    Path(settings.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    schema_sql = Path(settings.SCHEMA_PATH).read_text(encoding="utf-8")

    async with aiosqlite.connect(settings.DATABASE_PATH) as conn:
        await conn.executescript(schema_sql)
        await conn.commit()

    await _pool.init_pool()

    async with get_db() as db:
        existing_tpl = await db.fetchrow(
            "SELECT template_id FROM message_templates WHERE group_id IS NULL LIMIT 1"
        )
        if not existing_tpl:
            default_buttons = json.dumps(
                [[{"text": "📖 群规必读", "url": "https://telegram.org"}]],
                ensure_ascii=False,
            )
            await db.execute(
                """
                INSERT INTO message_templates (group_id, template_name, media_type, media_source, content, buttons_json)
                VALUES (NULL, ?, 'none', '', ?, ?)
                """,
                (
                    "系统默认入群欢迎模板",
                    "🎉 欢迎新成员 {mention} 加入 <b>{group_title}</b>！\n\n"
                    "🪪 您的 ID：<code>{user_id}</code>\n"
                    "📌 请遵守群规，文明交流，有问题可随时呼叫群管或 @AI 助手。",
                    default_buttons,
                ),
            )


async def ensure_group_initialized(
    group_id: int, group_title: str = "未命名群组"
) -> None:
    """
    确保指定群组在所有子配置表中均拥有默认初始化记录（幂等操作）
    """
    async with get_db() as db:
        existing = await db.fetchrow(
            "SELECT group_id FROM group_settings WHERE group_id = ?", (group_id,)
        )
        if not existing:
            await db.execute(
                "INSERT INTO group_settings (group_id, group_title, is_active) VALUES (?, ?, 1)",
                (group_id, group_title),
            )
        else:
            await db.execute(
                "UPDATE group_settings SET group_title = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (group_title, group_id),
            )

        # 获取默认模板 ID
        default_tpl = await db.fetchrow(
            "SELECT template_id FROM message_templates WHERE group_id = ? OR group_id IS NULL ORDER BY group_id DESC, template_id ASC LIMIT 1",
            (group_id,),
        )
        tpl_id = default_tpl["template_id"] if default_tpl else None

        await db.execute(
            "INSERT OR IGNORE INTO group_captcha_settings (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_welcome_settings (group_id, active_template_id) VALUES (?, ?)",
            (group_id, tpl_id),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_moderation_settings (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_llm_settings (group_id, base_url) VALUES (?, ?)",
            (group_id, settings.OLLAMA_BASE_URL),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_emergency_settings (group_id) VALUES (?)",
            (group_id,),
        )


async def record_audit_log(
    group_id: int, operator_id: int, operator_name: str, action_type: str, detail: str
) -> None:
    """
    写入管理员或系统审计日志
    """
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO admin_audit_logs (group_id, operator_id, operator_name, action_type, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, operator_id, operator_name, action_type, detail),
        )


async def close_db() -> None:
    await _pool.close_all()
