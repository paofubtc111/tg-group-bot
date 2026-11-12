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
    _is_ai_related,
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
    ok &= check("自定义：AI 收购仍直接发", _is_breaking("my-model 收购一家公司", high, models, events))
    ok &= check("自定义：无 AI 的收购不发", not _is_breaking("A 收购 B", high, models, events))
    ok &= check("自定义：只有模型不发", not _is_breaking("my-model 教程", high, models, events))

    # 6. AI 相关性门：36氪非 AI 商业新闻不再误发（pao 2026-11-12 截图的三条）
    ok &= check("误发1：华尔街监管不发", not _is_breaking("华尔街监管机构针对上市公司的执法案件降至16年低点"))
    ok &= check("误发2：京东收购零售不发", not _is_breaking("京东完成收购德国零售集团CECONOMY，斥资13.35亿欧元取得59.8%股权"))
    ok &= check("误发3：股票退市不发", not _is_breaking("萃华退：公司股票将于11月13日终止上市暨摘牌"))
    ok &= check("误发：普通融资不发", not _is_breaking("某餐饮品牌完成 2 亿元融资"))
    # AI 相关照常发
    ok &= check("AI 收购照常发", _is_breaking("OpenAI 收购一家 AI 芯片公司"))
    ok &= check("AI 融资照常发", _is_breaking("Anthropic 完成 10 亿美元融资，投向大模型"))
    ok &= check("AI 开源照常发", _is_breaking("Meta 开源新一代大模型"))
    ok &= check("AI 监管照常发", _is_breaking("欧盟通过 AI 监管法案，约束大模型"))
    ok &= check("AI 泄露照常发", _is_breaking("OpenAI 内部模型泄露，引发安全担忧"))
    ok &= check("AI 上市照常发", _is_breaking("某 AI 芯片公司上市，首日大涨"))
    # 第二档不受 AI 门影响（模型名本身就是 AI 信号）
    ok &= check("第二档：GPT-5 发布仍发", _is_breaking("GPT-5 正式发布"))
    ok &= check("第二档：只有模型不发", not _is_breaking("GPT-4 教程合集"))
    # 动态里的标准：开源权重、机器人模型、API 价格这类也要算 AI 相关
    ok &= check("动态标准：Praxis-1 开源权重发", _is_breaking("Praxis-1 开源权重上线"))
    ok &= check("动态标准：机器人模型发", _is_breaking("某机器人模型开源，具身智能新突破"))
    # OpenRouter 价格变动不算突发（价格类归早报/日报汇总，不单独推送），但要算 AI 相关
    ok &= check("动态标准：OpenRouter 价格算 AI 相关", _is_ai_related("OpenRouter 模型价格大降，glm 降价 70%"))
    ok &= check("动态标准：OpenRouter 价格不算突发", not _is_breaking("OpenRouter 模型价格大降，glm 降价 70%"))

    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
