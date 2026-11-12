# -*- coding: utf-8 -*-
"""AI 新闻面板配置测试：API GET/POST + 面板里有 AI 新闻模块。

跑法：
  NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    /tmp/venv-news/bin/python tests/test_ai_news_panel_20261112.py
用 DEV_MODE 绕过 Telegram 签名鉴权，只测本地逻辑。
"""

import os
import sys

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news_panel.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DEV_MODE"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

GROUP = -1004496842671


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def main():
    import asyncio
    if os.path.exists("/tmp/test_ai_news_panel.db"):
        os.remove("/tmp/test_ai_news_panel.db")
    from app.database.db import init_db
    asyncio.run(init_db())
    from app.main import app
    client = TestClient(app)
    ok = True

    # 1. 面板首页能打开，且包含 AI 新闻模块
    r = client.get("/")
    ok &= check("面板首页 200", r.status_code == 200)
    ok &= check("面板含 AI 新闻 section", 'id="view-ai-news"' in r.text)
    ok &= check("面板含 AI 新闻模块注册", "id: 'ai-news'" in r.text)
    ok &= check("面板含早报开关", 'id="ainews-daily-enabled"' in r.text)
    ok &= check("面板含早报时间", 'id="ainews-daily-time"' in r.text)
    ok &= check("面板含突发开关", 'id="ainews-breaking-enabled"' in r.text)
    ok &= check("面板含保存函数", "saveAINewsSettings" in r.text)
    ok &= check("面板含填充函数", "populateAINewsForm" in r.text)

    # 2. GET 默认配置（没行时给默认值）
    r = client.get(f"/api/group/{GROUP}/ai-news")
    ok &= check("GET ai-news 200", r.status_code == 200)
    data = r.json()
    ok &= check("默认早报关", data["ai_news"]["daily_enabled"] in (0, False))
    ok &= check("默认时间 09:00", data["ai_news"]["daily_time"] == "09:00")
    ok &= check("默认突发关", data["ai_news"]["breaking_enabled"] in (0, False))

    # 3. POST 保存（打开早报 08:30 + 突发）
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={"daily_enabled": True, "daily_time": "08:30", "breaking_enabled": True},
    )
    ok &= check("POST ai-news 200", r.status_code == 200)
    saved = r.json()["ai_news"]
    ok &= check("保存后早报开", saved["daily_enabled"] in (1, True))
    ok &= check("保存后时间 08:30", saved["daily_time"] == "08:30")
    ok &= check("保存后突发开", saved["breaking_enabled"] in (1, True))

    # 4. GET 读回保存的值
    r = client.get(f"/api/group/{GROUP}/ai-news")
    data = r.json()
    ok &= check("读回早报开", data["ai_news"]["daily_enabled"] in (1, True))
    ok &= check("读回时间 08:30", data["ai_news"]["daily_time"] == "08:30")

    # 5. 再 POST 改回关 + 换时间（upsert 不会重复建行）
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={"daily_enabled": False, "daily_time": "10:00", "breaking_enabled": False},
    )
    ok &= check("第二次 POST 200", r.status_code == 200)
    r = client.get(f"/api/group/{GROUP}/ai-news")
    data = r.json()
    ok &= check("改回后早报关", data["ai_news"]["daily_enabled"] in (0, False))
    ok &= check("改回后时间 10:00", data["ai_news"]["daily_time"] == "10:00")

    # 6. 非法时间格式被拒（422）
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={"daily_enabled": True, "daily_time": "25:99", "breaking_enabled": False},
    )
    ok &= check("非法时间 422", r.status_code == 422)

    print("\n全部通过" if ok else "\n有失败项")
    import asyncio as _aio
    from app.database.db import close_db
    _aio.run(close_db())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
