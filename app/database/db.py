import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

from app.config import settings

try:
    import asyncpg
except ImportError:  # asyncpg 仅 Postgres 路径需要; 不装也不影响 SQLite 运行
    asyncpg = None


# ======================================================================
# SQLite -> PostgreSQL 翻译层
# 业务代码全部按 SQLite 方言书写 (? 占位符 / LIKE / INSERT OR IGNORE /
# date(...)/datetime('now', ...) 等), PG 路径在调用时统一翻译, 调用点不用改。
# ======================================================================

def _try_parse_date_call(sql: str, paren: int, name: str):
    """解析 date(<arg>[, '<mod>'...]) / datetime('now', '<mod>')。

    参数可能是列名、'now', 修饰符是 '+8 hours' / '-30 days' 这类引号字符串。
    成功返回 (PG 表达式, 调用结束下标), 形状不符返回 None (原样保留)。
    """
    n = len(sql)
    i = paren + 1
    while i < n and sql[i] in " \t\n":
        i += 1
    if i < n and sql[i] == "'":
        j = i + 1
        while j < n and sql[j] != "'":
            j += 1
        if j >= n:
            return None
        arg = sql[i + 1:j]
        if arg.lower() != "now":
            return None
        i = j + 1
        arg_is_now = True
    else:
        j = i
        while j < n and (sql[j].isalnum() or sql[j] in "_."):
            j += 1
        if j == i or name == "datetime":
            return None
        arg = sql[i:j]
        i = j
        arg_is_now = False
    mods: list[tuple[str, str]] = []
    while True:
        while i < n and sql[i] in " \t\n":
            i += 1
        if i < n and sql[i] == ",":
            i += 1
            while i < n and sql[i] in " \t\n":
                i += 1
            if i < n and sql[i] == "'":
                j = i + 1
                while j < n and sql[j] != "'":
                    j += 1
                if j >= n:
                    return None
                m = re.fullmatch(r"([+-])(\d+\s+[a-zA-Z]+)", sql[i + 1:j])
                if not m:
                    return None
                mods.append((m.group(1), m.group(2)))
                i = j + 1
                continue
            return None
        break
    while i < n and sql[i] in " \t\n":
        i += 1
    if i >= n or sql[i] != ")":
        return None
    i += 1
    if name == "datetime":
        # 代码库里只有 datetime('now', '-30 days') 一种
        if len(mods) != 1:
            return None
        sign, val = mods[0]
        return f"now() {'+' if sign == '+' else '-'} interval '{val}'", i
    # date(列, '+8 hours') / date('now', '+8 hours', '-6 days')
    # 用 AT TIME ZONE 'UTC' 而不用会话时区, 保证结果与 SQLite 的 UTC
    # 字符串运算一致, 不受服务器时区设置影响。
    expr = "now() AT TIME ZONE 'UTC'" if arg_is_now else f"{arg} AT TIME ZONE 'UTC'"
    for sign, val in mods:
        expr = f"{expr} {'+' if sign == '+' else '-'} interval '{val}'"
    return f"({expr})::date", i


def _translate_sqlite_to_pg(sql: str) -> str:
    """把一条 SQLite 方言 SQL 翻译成 PostgreSQL 方言。

    单遍扫描: 单引号字面量 (含 '' 转义) 原样保留, 只翻译代码段,
    避免错翻字符串里的 ? / LIKE / date( 等。
    """
    out: list[str] = []
    counter = [0]
    insert_or_ignore = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == "?":
            counter[0] += 1
            out.append(f"${counter[0]}")
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            lw = word.lower()
            if lw == "insert":
                m = re.match(r"\s+OR\s+IGNORE\s+INTO\b", sql[j:], re.IGNORECASE)
                if m:
                    insert_or_ignore = True
                    out.append("INSERT INTO")
                    i = j + m.end()
                    continue
            elif lw == "like":
                # PG 的 LIKE 区分大小写, 用 ILIKE 保持与 SQLite 一致
                out.append("ILIKE")
                i = j
                continue
            elif lw in ("date", "datetime"):
                k = j
                while k < n and sql[k] in " \t\n":
                    k += 1
                if k < n and sql[k] == "(":
                    parsed = _try_parse_date_call(sql, k, lw)
                    if parsed is not None:
                        pg_expr, end = parsed
                        out.append(pg_expr)
                        i = end
                        continue
            out.append(word)
            i = j
            continue
        out.append(ch)
        i += 1
    translated = "".join(out)
    if insert_or_ignore:
        # INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING (追加到语句末尾)
        stripped = translated.rstrip()
        suffix = ""
        if stripped.endswith(";"):
            stripped = stripped[:-1].rstrip()
            suffix = ";"
        translated = stripped + " ON CONFLICT DO NOTHING" + suffix
    return translated


def _pg_value(value: Any) -> Any:
    """asyncpg 返回的日期/时间对象转成字符串, 与 SQLite 路径的返回保持一致。

    datetime -> 'YYYY-MM-DD HH:MM:SS+00:00' (isoformat, 空格分隔、带时区)
    date     -> 'YYYY-MM-DD'
    """
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _pg_row_to_dict(row: Any) -> dict[str, Any]:
    return {key: _pg_value(val) for key, val in dict(row).items()}


# schema_pg.sql 里的 TIMESTAMPTZ 列 (其余时间类列是 TEXT, 保持字符串即可)
_TIMESTAMPTZ_COLUMNS = frozenset(
    {"created_at", "updated_at", "last_seen", "joined_at", "last_violation_time", "expire_at"}
)


def _blank_sql_literals(sql: str) -> str:
    """把单引号字面量 (含 '' 转义) 替换成等长空格, 位置不变, 方便按位置分析 ?。"""
    chars = list(sql)
    i, n = 0, len(sql)
    while i < n:
        if sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, n)):
                chars[k] = " "
            i = j
        else:
            i += 1
    return "".join(chars)


def _split_top_level(s: str) -> list[str]:
    """按顶层逗号切分 (括号内的逗号不切, 字面量已先抹掉)。"""
    parts: list[str] = []
    depth, cur = 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _param_kinds(sql: str) -> list[Optional[str]]:
    """返回每个 ? 占位符的转换类型: None / 'datetime' / 'date'。

    asyncpg 按 PG 推断出的参数类型编码: TIMESTAMPTZ 列要求传 datetime 对象、
    date 比较要求传 date 对象, 传 ISO 字符串会直接报错, 所以要在调用层
    按 SQL 上下文把时间字符串转成对应对象:
    - INSERT 列清单 / UPDATE SET 里对应 TIMESTAMPTZ 列的 ? -> 'datetime'
    - date(...) = ? 里的 ? -> 'date'
    - WHERE <时间列> <比较符> ? -> 'datetime'
    其余 ? (含 TEXT 列的时间字符串, 如 last_post_at / start_at / end_at) 不动。
    """
    blanked = _blank_sql_literals(sql)
    qpos = [m.start() for m in re.finditer(r"\?", blanked)]
    kinds: list[Optional[str]] = [None] * len(qpos)
    if not qpos:
        return kinds
    qindex = {pos: i for i, pos in enumerate(qpos)}

    # 1) INSERT INTO t (列) VALUES (...): 按值位置映射到列
    m = re.search(
        r"INSERT\s+INTO\s+\"?\w+\"?\s*\(([^)]*)\)\s*VALUES\s*\(",
        blanked,
        re.I | re.S,
    )
    if m:
        cols = [c.strip().strip('"').lower() for c in m.group(1).split(",")]
        vopen = m.end() - 1  # VALUES 的 '(' 位置
        depth, i = 0, vopen
        while i < len(blanked):
            if blanked[i] == "(":
                depth += 1
            elif blanked[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        vclose = i
        inner_q = [p for p in qpos if vopen < p < vclose]
        vals = _split_top_level(blanked[vopen + 1:vclose])
        qi = 0
        for k, elem in enumerate(vals):
            if elem.strip() == "?":
                if k < len(cols) and cols[k] in _TIMESTAMPTZ_COLUMNS:
                    kinds[qindex[inner_q[qi]]] = "datetime"
                qi += 1

    # 2) UPDATE t SET 列 = ?, ...: 按列名映射
    m = re.search(
        r"\bUPDATE\s+\"?\w+\"?\s+SET\s+(.*?)(?:\bWHERE\b|$)", blanked, re.I | re.S
    )
    if m:
        set_clause, set_start = m.group(1), m.start(1)
        for am in re.finditer(r"([A-Za-z_]\w*)\s*=\s*\?", set_clause):
            if am.group(1).lower() in _TIMESTAMPTZ_COLUMNS:
                kinds[qindex[set_start + am.end() - 1]] = "datetime"

    # 3) date(...) = ? / ? = date(...): 日期比较
    for dm in re.finditer(r"date\s*\([^)]*\)\s*=\s*\?", blanked, re.I):
        kinds[qindex[dm.end() - 1]] = "date"
    for dm in re.finditer(r"\?\s*=\s*date\s*\(", blanked, re.I):
        kinds[qindex[dm.start()]] = "date"

    # 4) WHERE <时间列> <比较符> ?
    for cm in re.finditer(
        r"([A-Za-z_]\w*)\s*(?:=|<>|!=|<=|>=|<|>)\s*\?", blanked
    ):
        if cm.group(1).lower() in _TIMESTAMPTZ_COLUMNS:
            kinds[qindex[cm.end() - 1]] = "datetime"

    return kinds


def _coerce_pg_param(value: Any, kind: Optional[str]) -> Any:
    """按 _param_kinds 的判定, 把 ISO 时间字符串转成 datetime/date 对象。"""
    if kind is None or not isinstance(value, str):
        return value
    if kind == "date":
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_pg_params(query: str, params: tuple | list) -> tuple:
    kinds = _param_kinds(query)
    return tuple(
        _coerce_pg_param(v, kinds[i]) if i < len(kinds) else v
        for i, v in enumerate(params)
    )


# 自增主键表 -> 主键列 (execute() 在 PG 路径给 INSERT 追加 RETURNING 用)
_IDENTITY_PK: dict[str, str] = {
    "message_templates": "template_id",
    "pending_verifications": "id",
    "group_sensitive_rules": "rule_id",
    "user_warn_records": "id",
    "llm_chat_history": "id",
    "admin_audit_logs": "log_id",
    "group_messages": "id",
    "keyword_replies": "reply_id",
    "scheduled_messages": "id",
    "point_logs": "id",
    "point_rules": "rule_id",
    "point_redemptions": "id",
    "point_redemption_records": "id",
    "lotteries": "id",
    "lottery_prizes": "id",
    "lottery_entries": "id",
    "lottery_draws": "id",
    "lottery_plays": "id",
    "guesses": "id",
    "guess_options": "id",
    "guess_bets": "id",
    "dice_records": "id",
    "rotating_ads": "id",
    "invites": "id",
    "scheduled_lockdowns": "id",
    "lockdown_keywords": "id",
    "bottom_buttons": "id",
}
_INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+\"?([A-Za-z_]\w*)\"?", re.IGNORECASE
)


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


class AsyncPGSession:
    """
    封装 asyncpg 连接, 与 AsyncDBSession 同名方法, 供 get_db() 统一使用。
    内部把 SQLite 方言 SQL 翻译成 PG 方言再执行。
    """

    def __init__(self, conn: "asyncpg.Connection"):
        self.conn = conn

    async def fetch(self, query: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        pg_query = _translate_sqlite_to_pg(query)
        rows = await self.conn.fetch(pg_query, *_coerce_pg_params(query, params))
        return [_pg_row_to_dict(row) for row in rows]

    async def fetchrow(self, query: str, params: tuple | list = ()) -> Optional[dict[str, Any]]:
        pg_query = _translate_sqlite_to_pg(query)
        row = await self.conn.fetchrow(pg_query, *_coerce_pg_params(query, params))
        return _pg_row_to_dict(row) if row is not None else None

    async def execute(self, query: str, params: tuple | list = ()) -> int:
        pg_query = _translate_sqlite_to_pg(query)
        params = _coerce_pg_params(query, params)
        match = _INSERT_RE.match(pg_query)
        if match:
            table = match.group(1).lower()
            pk = _IDENTITY_PK.get(table)
            if pk and "RETURNING" not in pg_query.upper():
                # 自增主键表: 追加 RETURNING 取新 id (与 SQLite 的 lastrowid 对齐)
                pg_query = pg_query.rstrip().rstrip(";") + f" RETURNING {pk}"
                row = await self.conn.fetchrow(pg_query, *params)
                if row is None:  # ON CONFLICT DO NOTHING 冲突时无行返回
                    return 0
                return int(row[pk])
        # UPDATE / DELETE / 无自增主键的 INSERT: 解析状态串取影响行数
        status = await self.conn.execute(pg_query, *params)
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def executemany(self, query: str, seq_of_params: list[tuple | list]) -> None:
        pg_query = _translate_sqlite_to_pg(query)
        await self.conn.executemany(
            pg_query, [_coerce_pg_params(query, p) for p in seq_of_params]
        )


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

# ---- PostgreSQL 连接池 (DATABASE_URL 设置时启用) ----
_pg_pool: Optional["asyncpg.Pool"] = None
_pg_pool_lock = asyncio.Lock()


def _use_pg() -> bool:
    return bool(settings.DATABASE_URL)


async def _get_pg_pool() -> "asyncpg.Pool":
    global _pg_pool
    if _pg_pool is None:
        async with _pg_pool_lock:
            if _pg_pool is None:
                if asyncpg is None:
                    raise RuntimeError(
                        "DATABASE_URL 已设置, 但未安装 asyncpg, 请先 pip install asyncpg"
                    )
                _pg_pool = await asyncpg.create_pool(
                    settings.DATABASE_URL, min_size=1, max_size=5
                )
    return _pg_pool


@asynccontextmanager
async def get_db() -> AsyncIterator[Any]:
    """
    获取异步数据库会话上下文管理器。
    DATABASE_URL 设置时用 asyncpg 连接池 (PostgreSQL), 否则走 aiosqlite。
    """
    if _use_pg():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            yield AsyncPGSession(conn)
    else:
        conn = await _pool.acquire()
        try:
            yield AsyncDBSession(conn)
        finally:
            await _pool.release(conn)


# 已存在表的新增列 (schema 已同步; 迁移只给旧库补列, 幂等)
_NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("lockdown_keywords", "allow_admin_trigger", "INTEGER DEFAULT 0"),
    ("group_moderation_settings", "moderate_admins", "INTEGER DEFAULT 0"),
)


async def _migrate_sqlite_columns(conn: aiosqlite.Connection) -> None:
    """SQLite: 先查 PRAGMA table_info 再 ADD COLUMN (不支持 IF NOT EXISTS)。"""
    for table, column, ddl in _NEW_COLUMNS:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await conn.commit()


async def _migrate_pg_columns(conn: "asyncpg.Connection") -> None:
    """PostgreSQL: ADD COLUMN IF NOT EXISTS。"""
    for table, column, ddl in _NEW_COLUMNS:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")


async def _ensure_default_template() -> None:
    """确保系统默认全局欢迎模板存在 (SQLite / PG 共用)。"""
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


async def init_db() -> None:
    """
    初始化数据库 Schema 并确保默认全局欢迎模板存在
    """
    if _use_pg():
        pool = await _get_pg_pool()
        schema_sql = Path(settings.SCHEMA_PATH).with_name("schema_pg.sql").read_text(
            encoding="utf-8"
        )
        async with pool.acquire() as conn:
            # asyncpg 的 execute() 支持一次执行多条语句
            await conn.execute(schema_sql)
            await _migrate_pg_columns(conn)
        await _ensure_default_template()
        return

    Path(settings.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    schema_sql = Path(settings.SCHEMA_PATH).read_text(encoding="utf-8")

    async with aiosqlite.connect(settings.DATABASE_PATH) as conn:
        await conn.executescript(schema_sql)
        await conn.commit()
        await _migrate_sqlite_columns(conn)

    await _pool.init_pool()

    await _ensure_default_template()


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
        elif group_title != "未命名群组":
            # 仅当调用方传入了真实群名时才覆盖标题；API 端点传入默认
            # "未命名群组" 时不得抹掉中间件已写入的真实群名。
            # 机器人能收到这个群的消息, 就说明它还是群成员、群是活跃的:
            # 已存在行也把 is_active 翻回 1 (被踢了就收不到消息, 中间件
            # 根本不会跑, 所以翻回是安全的; 踢出时的置 0 由
            # on_bot_member_updated 的 my_chat_member 处理负责)。
            await db.execute(
                "UPDATE group_settings SET group_title = ?, is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (group_title, group_id),
            )
        else:
            await db.execute(
                "UPDATE group_settings SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (group_id,),
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
        # Batch 2: 积分 / 骰子 / 轮播广告 / 退群拉黑 / 改名监控 / 其他设置 (单行配置表)
        await db.execute(
            "INSERT OR IGNORE INTO point_settings (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO dice_settings (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO rotating_ads_settings (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_leave_ban (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_rename_watch (group_id) VALUES (?)",
            (group_id,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO group_misc_settings (group_id) VALUES (?)",
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
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
    await _pool.close_all()
