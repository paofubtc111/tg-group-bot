"""Muse 动态导入接口测试（2026-11-12）。
覆盖：单元转入库条目（取首链接、去 Markdown、摘要清洗、无链接用单元 id 去重）、
批量导入去重、接口鉴权（无 token 401、未配 token 503、正确 token 200 入库）。
运行：NO_PROXY=... ~/workspace/.venv-bot-test/bin/python -u tests/test_ai_news_feedimport_20261112.py
"""
import os
import sys
import tempfile

os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"
TMP = tempfile.mkdtemp(prefix="feedimport-test-")
os.environ["DATABASE_PATH"] = os.path.join(TMP, "bot.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bot.handlers.ai_news import feed_unit_to_item, import_feed_units
from app.config import settings
from app.database.db import close_db, init_db

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {name}")
    else:
        failed += 1
        print(f"FAIL {name}")


async def main():
    global passed, failed
    await init_db()
    try:
        # 1. 单元转条目：取正文第一个原文链接，摘要去 Markdown
        it = feed_unit_to_item({
            "title": "Praxis-1 开源权重上线",
            "body_md": "Runway 11 月 11 日把 [Praxis-1](https://runway.com/news/company-news/praxis-1-is-now-available) 的权重以 Apache 2.0 协议开放了。它在 NVIDIA RoboLab-120 上达到 46.2% 成功率。",
            "source": "Muse动态",
            "published_at": "2026-11-12",
            "unit_id": "a4ba8ed5-8ba8-4138-b4aa-45380c6adf85",
        })
        check("取首链接", it and it["link"] == "https://runway.com/news/company-news/praxis-1-is-now-available")
        check("标题原样", it and it["title"] == "Praxis-1 开源权重上线")
        check("来源 Muse动态", it and it["source"] == "Muse动态")
        check("摘要去 Markdown 链接", it and "Praxis-1" in it["summary"] and "runway.com" not in it["summary"])
        check("摘要含关键信息", it and "46.2%" in it["summary"])
        check("发布日期保留", it and it["published_at"] == "2026-11-12")

        # 2. 无链接时用单元 id 做去重键
        it2 = feed_unit_to_item({
            "title": "无链接的动态", "body_md": "纯文字，没有任何链接。",
            "unit_id": "unit-no-link-1",
        })
        check("无链接用单元 id", it2 and it2["link"] == "muse-feed://unit-no-link-1")
        check("无标题返回 None", feed_unit_to_item({"body_md": "x"}) is None)
        check("无标题无 id 返回 None", feed_unit_to_item({"title": "t", "body_md": "无链接"}) is None)

        # 3. 批量导入：去重（同链接只入一次）
        res = await import_feed_units([
            {"title": "Anthropic 拟募资 904 亿美元上市，投资者抢购",
             "body_md": "[路演](https://newseisamay.com/business/industry/anthropic-ipo-90-billion-investor-demand)开始后不久，认购订单就已全额覆盖。",
             "source": "Muse动态", "published_at": "2026-11-12", "unit_id": "u1"},
            {"title": "Anthropic 拟募资 904 亿美元上市，投资者抢购",
             "body_md": "[路演](https://newseisamay.com/business/industry/anthropic-ipo-90-billion-investor-demand)开始后不久，认购订单就已全额覆盖。",
             "source": "Muse动态", "published_at": "2026-11-12", "unit_id": "u1"},
            {"title": "Praxis-1 开源权重上线",
             "body_md": "Runway 11 月 11 日把 [Praxis-1](https://runway.com/news/company-news/praxis-1-is-now-available) 的权重开放了。",
             "source": "Muse动态", "published_at": "2026-11-12", "unit_id": "u2"},
        ])
        check("批量导入 2 条新", res["imported"] == 2)
        check("批量导入 1 条重复", res["duplicates"] == 1)
        check("收到 3 条", res["received"] == 3)

        # 4. 接口鉴权 + 入库（TestClient）
        from fastapi.testclient import TestClient
        from app.main import app

        settings.FEED_IMPORT_TOKEN = "test-feed-token-123"
        with TestClient(app) as client:
            r = client.post("/api/ai-news/feed-import", json={"units": []})
            check("无 token 401", r.status_code == 401)
            r = client.post("/api/ai-news/feed-import", json={"units": []},
                            headers={"X-Feed-Import-Token": "wrong"})
            check("错 token 401", r.status_code == 401)
            r = client.post("/api/ai-news/feed-import", json={"units": [
                {"title": "OpenRouter 三款模型涨价约 142%",
                 "body_md": "同一份周报里，[OpenRouter](https://www.computeunion.net/weekly) 的 glm-5-3-flash 从每百万输入 0.15 美元降至 0.0399 美元。",
                 "source": "Muse动态", "published_at": "2026-11-12", "unit_id": "u3"},
            ]}, headers={"X-Feed-Import-Token": "test-feed-token-123"})
            check("正确 token 200", r.status_code == 200)
            check("接口返回 imported=1", r.json().get("imported") == 1)
            # 再发一次同一条，应为重复
            r = client.post("/api/ai-news/feed-import", json={"units": [
                {"title": "OpenRouter 三款模型涨价约 142%",
                 "body_md": "同一份周报里，[OpenRouter](https://www.computeunion.net/weekly) 的 glm-5-3-flash 从每百万输入 0.15 美元降至 0.0399 美元。",
                 "source": "Muse动态", "published_at": "2026-11-12", "unit_id": "u3"},
            ]}, headers={"X-Feed-Import-Token": "test-feed-token-123"})
            check("重复导入 duplicates=1", r.json().get("duplicates") == 1)
        settings.FEED_IMPORT_TOKEN = ""
        r = client.post("/api/ai-news/feed-import", json={"units": []},
                        headers={"X-Feed-Import-Token": "x"})
        check("未配 token 503", r.status_code == 503)
        settings.FEED_IMPORT_TOKEN = "test-feed-token-123"
    finally:
        await close_db()

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    print("全部通过" if failed == 0 else "有失败项")
    return failed == 0


if __name__ == "__main__":
    import asyncio

    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
