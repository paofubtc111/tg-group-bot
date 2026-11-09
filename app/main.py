import asyncio
import contextlib
import logging
from typing import Optional

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.bot.handlers import setup_bot_routers
from app.bot.handlers.captcha import recover_pending_verifications
from app.bot.handlers.group_features import scheduled_sender_loop
from app.bot.middlewares.group_tracker import GroupTrackerMiddleware
from app.config import settings
from app.database.db import close_db, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("tg-group-bot")


def create_bot_and_dispatcher() -> tuple[Optional[Bot], Dispatcher]:
    dp = Dispatcher()
    dp.update.outer_middleware(GroupTrackerMiddleware())
    dp.include_router(setup_bot_routers())

    if not settings.BOT_TOKEN or ":" not in settings.BOT_TOKEN:
        logger.warning(
            "未检测到合法的 BOT_TOKEN，将以仅 WebApp 面板预览模式启动（不连接 Telegram Polling）。"
        )
        return None, dp

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return bot, dp


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    bot, dp = create_bot_and_dispatcher()
    app.state.bot = bot
    app.state.dp = dp

    polling_task: Optional[asyncio.Task] = None
    scheduler_task: Optional[asyncio.Task] = None
    if bot is not None:
        try:
            await recover_pending_verifications(bot)
            if settings.WEBAPP_BASE_URL.startswith("https://"):
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="🎛️ 群管面板",
                        web_app=WebAppInfo(url=settings.WEBAPP_BASE_URL),
                    )
                )
        except Exception as exc:
            logger.warning("初始化 Bot 菜单或恢复待验证任务异常: %s", exc)

        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_signals=False,
            )
        )
        logger.info("aiogram v3 Bot Polling 协程已启动")

        # 定时发送后台任务 (仅 Bot 在线时启动, 预览模式跳过)
        scheduler_task = asyncio.create_task(scheduled_sender_loop(bot))

    try:
        yield
    finally:
        if scheduler_task and not scheduler_task.done():
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
        if polling_task and not polling_task.done():
            polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling_task
        if bot is not None:
            await bot.session.close()
        await close_db()
        logger.info("系统资源已安全释放")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Telegram 群管与本地大模型交互 Bot 控制面板 API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.WEBAPP_PORT,
        reload=False,
    )
