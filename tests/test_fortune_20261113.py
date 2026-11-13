# -*- coding: utf-8 -*-
"""今日运势测试（SQLite 本地）：触发、一天全局一种、开关、档位配置、面板接线。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    ~/workspace/.venv-bot-test/bin/python tests/test_fortune_20261113.py

规则（pao 2026-11-13 定）：消息带“运势”就抽；默认四档各 25%；
同一用户一天全局一种（北京时间 00:00 分界），重复问返回同一结果；
面板可开关、可配档位与话术；群聊与私聊都触发。
"""
import asyncio
import os
import random
import sys
from datetime import datetime, timezone

os.environ["DATABASE_PATH"] = "/tmp/test_fortune.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.types import Chat, Message, User
from pydantic import ValidationError

from app.api.routes import FortuneConfigPayload
from app.bot.handlers.fortune import (
    DEFAULT_FORTUNE_TIERS,
    _clean_tiers,
    check_daily_fortune,
    draw_fortune,
    get_fortune_config,
)
from app.database.db import close_db, get_db, init_db

GROUP = -1004496842671
GROUP2 = -100999888777
MEMBER = 111222333


class FakeBot:
    """只记录 send_message，不发真请求。"""

    def __init__(self):
        self.sent = []

    async def __call__(self, method):
        # aiogram 3：Message.reply/answer 走 bot(method) 发送
        self.sent.append({
            "chat_id": getattr(method, "chat_id", None),
            "text": getattr(method, "text", ""),
        })
        return None

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return None


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def fortune_message(text, chat_id=GROUP, chat_type="supergroup", user_id=MEMBER, bot=None):
    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type, title="拥抱A i"),
        from_user=User(id=user_id, is_bot=False, first_name="小明", username="xiaoming"),
        text=text,
    )
    return msg.as_(bot) if bot is not None else msg


async def fresh_db():
    await close_db()
    if os.path.exists("/tmp/test_fortune.db"):
        os.remove("/tmp/test_fortune.db")
    await init_db()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_settings (group_id, group_title) VALUES (?, '拥抱A i')",
            (GROUP,),
        )


async def test_default_tiers_match_pao():
    """默认四档 = pao 定的档位、权重与话术（逐字）。"""
    ok = True
    ok &= check("默认四档", len(DEFAULT_FORTUNE_TIERS) == 4)
    names = [t["name"] for t in DEFAULT_FORTUNE_TIERS]
    ok &= check("档位名 大吉/吉/凶/大凶", names == ["大吉", "吉", "凶", "大凶"])
    ok &= check("权重都 25", all(t["weight"] == 25 for t in DEFAULT_FORTUNE_TIERS))
    lines = {t["name"]: t["lines"][0] for t in DEFAULT_FORTUNE_TIERS}
    ok &= check("大吉话术", lines["大吉"] == "总裁您今天吉星高照，运势腾达，天降横财。")
    ok &= check("吉话术", lines["吉"] == "今天手气不错，把握住小机会，小赚一笔。")
    ok &= check("凶话术", lines["凶"] == "今天水逆，注意小人，别被害了，稳住别浪。")
    ok &= check("大凶话术", lines["大凶"] == "总裁您今日诸事不宜，适合躺平，钱包捂紧，明天再战。")
    return ok


async def test_trigger_and_same_day():
    """带“运势”触发；同一天重复问返回同一结果。"""
    await fresh_db()
    bot = FakeBot()
    ok = True
    msg = fortune_message("今天运势怎么样", bot=bot)
    ok &= check("触发返回 True", await check_daily_fortune(msg, bot) is True)
    ok &= check("发了一条回复", len(bot.sent) == 1)
    first = bot.sent[0]["text"] if bot.sent else ""
    ok &= check("回复带今日运势", "今日运势：" in first)
    ok &= check("回复带一档档位名",
                any(n in first for n in ["大吉", "吉", "凶", "大凶"]))
    msg2 = fortune_message("再问一次运势", bot=bot)
    ok &= check("重复问也返回 True", await check_daily_fortune(msg2, bot) is True)
    ok &= check("重复问结果完全相同",
                len(bot.sent) == 2 and bot.sent[1]["text"] == first)
    return ok


async def test_no_trigger():
    await fresh_db()
    bot = FakeBot()
    ok = True
    ok &= check("不带运势不触发",
                await check_daily_fortune(fortune_message("今天天气不错", bot=bot), bot) is False)
    ok &= check("没发消息", len(bot.sent) == 0)
    return ok


async def test_global_once_per_day():
    """同一用户换个群问，仍是同一运势（全局一种，不分群）。"""
    await fresh_db()
    bot = FakeBot()
    ok = True
    await check_daily_fortune(fortune_message("运势", bot=bot), bot)
    first = bot.sent[0]["text"] if bot.sent else ""
    await check_daily_fortune(
        fortune_message("运势", chat_id=GROUP2, bot=bot), bot)
    ok &= check("换群结果相同",
                len(bot.sent) == 2 and bot.sent[1]["text"] == first)
    return ok


async def test_disabled_group():
    """群关掉开关后不触发。"""
    await fresh_db()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO group_fortune_settings (group_id, enabled) VALUES (?, 0)",
            (GROUP,),
        )
    bot = FakeBot()
    ok = True
    ok &= check("关闭后不触发",
                await check_daily_fortune(fortune_message("运势", bot=bot), bot) is False)
    ok &= check("没发消息", len(bot.sent) == 0)
    return ok


async def test_private_chat():
    """私聊带“运势”也触发（用默认配置）。"""
    await fresh_db()
    bot = FakeBot()
    ok = True
    msg = fortune_message("运势", chat_id=MEMBER, chat_type="private", bot=bot)
    ok &= check("私聊触发", await check_daily_fortune(msg, bot) is True)
    ok &= check("私聊发了一条", len(bot.sent) == 1)
    return ok


async def test_draw_distribution_and_clean():
    """加权抽签：固定种子下四档都会出现；脏配置回默认。"""
    await fresh_db()
    ok = True
    random.seed(7)
    tiers = await get_fortune_config(GROUP)
    seen = set()
    for i in range(40):
        tier, _line = await draw_fortune(1000 + i, tiers[1])
        seen.add(tier)
    ok &= check("40 次抽到全部四档", seen == {"大吉", "吉", "凶", "大凶"})
    ok &= check("脏配置回默认", _clean_tiers(None) == _clean_tiers([]))
    ok &= check("非法档位被丢弃",
                len(_clean_tiers([{"name": "", "weight": 25, "lines": ["x"]},
                                  {"name": "吉", "weight": 0, "lines": ["y"]}])) == 4)
    return ok


async def test_payload_validation():
    ok = True
    try:
        FortuneConfigPayload(enabled=True, tiers=[
            {"name": "大吉", "weight": 25, "lines": ["好运。"]},
        ])
        ok &= check("合法配置通过", True)
    except ValidationError:
        ok &= check("合法配置通过", False)
    for bad, label in [
        ({"enabled": True, "tiers": []}, "空档位列表拒绝"),
        ({"enabled": True, "tiers": [{"name": "吉", "weight": 0, "lines": ["x"]}]}, "权重 0 拒绝"),
        ({"enabled": True, "tiers": [{"name": "吉", "weight": 25, "lines": []}]}, "空话术拒绝"),
    ]:
        try:
            FortuneConfigPayload(**bad)
            ok &= check(label, False)
        except ValidationError:
            ok &= check(label, True)
    return ok


async def test_frontend_and_hook():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "app/web/templates/index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    ok = True
    ok &= check("面板有运势视图", 'id="view-fortune"' in html)
    ok &= check("面板有开关与档位列表",
                'id="fortune-enabled"' in html and 'id="fortune-tiers-list"' in html)
    ok &= check("模块注册有 fortune", "id: 'fortune'" in html)
    ok &= check("路由列表有 fortune", "'fortune'" in html)
    ok &= check("预览 mock 有 fortune", "sub === 'fortune'" in html)
    llm_path = os.path.join(os.path.dirname(__file__), "..",
                            "app/bot/handlers/llm_chat.py")
    with open(llm_path, encoding="utf-8") as f:
        llm_src = f.read()
    ok &= check("管道已接 check_daily_fortune", "check_daily_fortune" in llm_src)
    return ok


async def main():
    results = []
    results.append(await test_default_tiers_match_pao())
    results.append(await test_trigger_and_same_day())
    results.append(await test_no_trigger())
    results.append(await test_global_once_per_day())
    results.append(await test_disabled_group())
    results.append(await test_private_chat())
    results.append(await test_draw_distribution_and_clean())
    results.append(await test_payload_validation())
    results.append(await test_frontend_and_hook())
    total = sum(1 for r in results if r)
    print(f"\n{total}/{len(results)} 组测试通过")
    await close_db()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
