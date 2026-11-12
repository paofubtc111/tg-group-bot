import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    WEBAPP_PORT: int = int(os.getenv("WEBAPP_PORT", os.getenv("PORT", "8080")))
    WEBAPP_BASE_URL: str = os.getenv("WEBAPP_BASE_URL", "https://localhost:8080").rstrip("/")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "bot.db"))
    # 设为 Postgres 连接串 (如 Neon 的 postgresql://...) 时用 asyncpg 连接外部库;
    # 为空时继续用本地 SQLite (DATABASE_PATH), 本地开发不受影响。
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    SCHEMA_PATH: str = str(Path(__file__).resolve().parent / "database" / "schema.sql")
    TEMPLATES_DIR: str = str(Path(__file__).resolve().parent / "web" / "templates")
    OLLAMA_MAX_CONCURRENCY: int = int(os.getenv("OLLAMA_MAX_CONCURRENCY", "2"))
    OLLAMA_TIMEOUT_SECONDS: float = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "60.0"))
    DEV_MODE: bool = os.getenv("DEV_MODE", "false").lower() in ("1", "true", "yes")
    DEV_ADMIN_ID: int = int(os.getenv("DEV_ADMIN_ID", "10000001"))
    # Muse 动态导入接口的共享密钥（X-Feed-Import-Token）。为空时该接口不可用。
    FEED_IMPORT_TOKEN: str = os.getenv("FEED_IMPORT_TOKEN", "")


settings = Settings()
