# -*- coding: utf-8 -*-
"""突发关键词可配置测试：解析、默认回退、每群自定义。

跑法：
  /home/hatch/workspace/.venv-bot-test/bin/python tests/test_ai_news_keywords_20261112.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DATABASE_PATH"] = "/tmp/test_ai_news_kw.db"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"

from app.bot.handlers.ai_news import (
    BREAKING_EVENTS,
    BREAKING_HIGH_SIGNAL,
    BREAKING_MODELS,
    _breaking_keywords,
    _is_breaking,
    _parse_keywords,
)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def main():
    ok = True

    # 1. 解析：中英文逗号、顿号、去空格、去重
    ok &= check("解析英文逗号", _parse_keywords("a, b ,c") == ["a", "b", "c"])
    ok &= check("解析中文逗号顿号", _parse_keywords("收购，并购、融资") == ["收购", "并购", "融资"])
    ok &= check("解析去重（大小写）", _parse_keywords("GPT-5, gpt-5, Claude") == ["GPT-5", "Claude"])
    ok &= check("解析空串", _parse_keywords("") == [])
    ok &= check("解析 None", _parse_keywords(None) == [])

    # 2. 默认回退：没配（空串）用模块默认值
    high, models, events = _breaking_keywords({})
    ok &= check("空配置用默认高信号", high == BREAKING_HIGH_SIGNAL)
    ok &= check("空配置用默认模型", models == BREAKING_MODELS)
    ok &= check("空配置用默认事件", events == BREAKING_EVENTS)
    high, models, events = _breaking_keywords({"breaking_high_signal": "", "breaking_models": "", "breaking_events": ""})
    ok &= check("空串配置用默认", high == BREAKING_HIGH_SIGNAL and models == BREAKING_MODELS)

    # 3. 自定义：只改一档，其他档仍用默认
    high, models, events = _breaking_keywords({"breaking_models": "my-model, qwen"})
    ok &= check("只改模型档", models == ["my-model", "qwen"])
    ok &= check("未改档仍默认", high == BREAKING_HIGH_SIGNAL and events == BREAKING_EVENTS)

    # 4. 默认词判定（和原来行为一致）
    ok &= check("默认：收购直接发", _is_breaking("OpenAI 收购一家芯片公司"))
    ok &= check("默认：模型+事件发", _is_breaking("GPT-5 正式发布"))
    ok &= check("默认：只有模型不发", not _is_breaking("GPT-4 教程合集"))
    ok &= check("默认：只有事件不发", not _is_breaking("某产品发布了新功能"))
    ok &= check("默认：开源直接发", _is_breaking("Meta 开源新模型"))

    # 5. 自定义词判定：自定义模型名 + 自定义事件词
    high, models, events = _breaking_keywords({
        "breaking_high_signal": "收购",
        "breaking_models": "my-model",
        "breaking_events": "发布",
    })
    ok &= check("自定义：my-model 发布发", _is_breaking("my-model 正式发布", high, models, events))
    ok &= check("自定义：GPT-5 发布不发（模型不在自定义里）", not _is_breaking("GPT-5 正式发布", high, models, events))
    ok &= check("自定义：收购仍直接发", _is_breaking("A 收购 B", high, models, events))
    ok &= check("自定义：只有模型不发", not _is_breaking("my-model 教程", high, models, events))

    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
