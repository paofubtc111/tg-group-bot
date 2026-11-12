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

    # 6. 关键词：保存自定义词，读回一致；留空 = 默认
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={
            "daily_enabled": True, "daily_time": "09:00", "breaking_enabled": True,
            "breaking_high_signal": "收购, 并购, 融资",
            "breaking_models": "gpt-5, claude, qwen",
            "breaking_events": "发布, 上线",
        },
    )
    ok &= check("POST 关键词 200", r.status_code == 200)
    r = client.get(f"/api/group/{GROUP}/ai-news")
    data = r.json()
    ok &= check("读回高信号词", data["ai_news"]["breaking_high_signal"] == "收购, 并购, 融资")
    ok &= check("读回模型名", data["ai_news"]["breaking_models"] == "gpt-5, claude, qwen")
    ok &= check("读回事件词", data["ai_news"]["breaking_events"] == "发布, 上线")

    # 6b. 每日上限：保存 5 条，读回 5 条
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={
            "daily_enabled": True, "daily_time": "09:00", "breaking_enabled": True,
            "breaking_high_signal": "", "breaking_models": "", "breaking_events": "",
            "max_breaking_per_day": 5,
        },
    )
    ok &= check("POST 上限 200", r.status_code == 200)
    r = client.get(f"/api/group/{GROUP}/ai-news")
    ok &= check("读回上限 5", r.json()["ai_news"]["max_breaking_per_day"] == 5)
    # 上限越界被拒（0 和 31 都 422；11 现在合法）
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={"daily_enabled": True, "daily_time": "09:00", "breaking_enabled": True,
              "max_breaking_per_day": 0},
    )
    ok &= check("上限 0 被拒 422", r.status_code == 422)
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={"daily_enabled": True, "daily_time": "09:00", "breaking_enabled": True,
              "max_breaking_per_day": 31},
    )
    ok &= check("上限 31 被拒 422", r.status_code == 422)
    r = client.post(
        f"/api/group/{GROUP}/ai-news",
        json={"daily_enabled": True, "daily_time": "09:00", "breaking_enabled": True,
              "max_breaking_per_day": 11},
    )
    ok &= check("上限 11 现在合法", r.status_code == 200)

    # 7. 面板含关键词输入框
    r = client.get("/")
    ok &= check("面板含高信号词输入框", 'id="ainews-kw-high"' in r.text)
    ok &= check("面板含模型名输入框", 'id="ainews-kw-models"' in r.text)
    ok &= check("面板含事件词输入框", 'id="ainews-kw-events"' in r.text)
    ok &= check("面板 allViews 含 ai-news", "'ai-news'" in r.text)
    ok &= check("面板含上限输入框", 'id="ainews-max-breaking"' in r.text)
    ok &= check("面板含测试早报按钮", 'sendTestDigest()' in r.text)

    # 8. 非法时间格式被拒（422）
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
