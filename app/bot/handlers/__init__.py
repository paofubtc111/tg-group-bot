from aiogram import Router
from app.bot.handlers.captcha import router as captcha_router
from app.bot.handlers.games import router as games_router
from app.bot.handlers.group_features import router as group_features_router
from app.bot.handlers.llm_chat import router as llm_chat_router
from app.bot.handlers.moderation import router as moderation_router
from app.bot.middlewares.group_tracker import tracker_router


def setup_bot_routers() -> Router:
    root_router = Router(name="root_bot_router")
    root_router.include_router(tracker_router)
    root_router.include_router(captcha_router)
    root_router.include_router(group_features_router)
    root_router.include_router(games_router)
    root_router.include_router(moderation_router)
    root_router.include_router(llm_chat_router)
    return root_router
