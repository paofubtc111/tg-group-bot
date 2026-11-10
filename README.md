# Telegram 群管与本地大模型交互 Bot (`tg-group-bot`)

对标 `@nmnmfunbot` 与 `@fangzhang_bot` 的高性能 Telegram 群组自动化管理与本地 Ollama 大模型智能交互系统。采用 **aiogram v3 + FastAPI + SQLite / PostgreSQL + 单文件 Tailwind/WebApp 控制面板** 架构，支持 Docker Compose 一键容器化交付。默认用本地 SQLite；设置 `DATABASE_URL`（如免费 Neon Postgres 连接串）后改用外部 Postgres，重新部署不再丢配置。

---

## 核心特性

1. **🎛️ Telegram Mini App 可视化后台（零构建单文件 HTML）**
   - 原生支持 `X-Telegram-Init-Data` HMAC-SHA256 签名校验与 `get_chat_administrators` 动态管理员越权防护。
   - 顶部实时搜索过滤 + 8 大核心功能模块卡片 + 底部悬浮多群组无缝切换条。
2. **🧩 入群人机验证网关 (CAPTCHA)**
   - 新成员进群立即调用 `restrictChatMember` 剥夺发言与媒体权限。
   - 支持 **点选模式 (`click`)**、**四则算术模式 (`math`)**、**图符匹配模式 (`choice`)**。
   - 他人点击防篡改弹窗提醒；超时未验证自动踢出 (`kick`) 或封禁 (`ban`) 并自动清理验证卡片；支持进程重启后恢复倒计时。
3. **💌 独立消息模板库与防刷屏入群欢迎**
   - 支持 `{username}`、`{fullname}`、`{mention}`、`{user_id}`、`{group_title}` 动态插值替换。
   - 支持纯文本、图片 (`photo`)、动图 (`animation`) 及多行多列 Inline URL 按钮矩阵。
   - 支持 **撤回上一条欢迎语 (`clean_previous`)** 与 **TTL 到期自动销毁 (`delete_ttl`)**。
4. **🛡️ 内容安全审计与阶梯处罚**
   - 管理员发言自动豁免白名单。
   - 支持拦截 `t.me` 链接、全部外部 URL、外部频道/用户转发消息。
   - 支持包含 (`contains`)、精确 (`exact`)、正则 (`regex`) 三类敏感词匹配及批量导入。
   - 违规秒删 + 告警消息定时销毁 + 累计警告次数阶梯升级处罚（禁言/踢出/封禁）。
5. **🤖 本地 Ollama 大模型接入（防爆显存并发锁）**
   - 内置 `asyncio.Semaphore(2)` 并发排队锁，防止多群高并发请求打爆本地 GPU 显存。
   - 支持 `@Bot` 提及、引用回复 (Reply)、自定义关键词前缀三种群聊唤醒方式。
   - 支持 **按用户隔离 (`user_isolated`)** 与 **全群共享 (`shared`)** 两种上下文记忆模式，服务离线或超时自动友好降级。
6. **⚡ 紧急模式、操作审计日志与配置一键克隆**
   - 支持一键全员禁言锁群 (`lockdown_mode`)、防拉人爆群自动移出 (`raid_protection`)、`/admin` 呼叫群管开关。
   - 全程记录管理员与系统自动化审计流水日志，支持 JSON 格式配置一键导出与跨群克隆。

---

## 快速启动

### 1. Docker Compose 一键部署（推荐）

```bash
cp .env.example .env
# 编辑 .env 填入您的 BOT_TOKEN 与 WEBAPP_BASE_URL (需 HTTPS 域名供 Telegram Mini App 调用)
docker compose up -d --build
```

### 2. 本地 Python 环境运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python -m app.main
```

启动后访问 `http://localhost:8080` 即可在浏览器预览控制面板（设置 `DEV_MODE=true` 可在本地浏览器免 Telegram 客户端签名直接调试全部 API）。
