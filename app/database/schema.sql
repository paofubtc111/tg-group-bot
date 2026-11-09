PRAGMA foreign_keys = ON;

-- 1. 群组核心配置表
CREATE TABLE IF NOT EXISTS group_settings (
    group_id INTEGER PRIMARY KEY,
    group_title TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 独立消息模板库表 (解耦复用)
CREATE TABLE IF NOT EXISTS message_templates (
    template_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,                        -- 允许为 NULL (系统全局模板) 或指定群专属
    template_name TEXT NOT NULL,             -- 模板名称 (例如: 新手日常欢迎语)
    media_type TEXT DEFAULT 'none',          -- 'none', 'photo', 'animation'
    media_source TEXT,                       -- 图片外链 URL 或 TG file_id
    content TEXT NOT NULL,                   -- 消息正文 (支持 HTML 与插值占位符)
    buttons_json TEXT DEFAULT '[]',          -- 按钮矩阵 JSON: [[{"text":"官网","url":"https://..."}]]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 3. 入群验证配置表
CREATE TABLE IF NOT EXISTS group_captcha_settings (
    group_id INTEGER PRIMARY KEY,
    enabled BOOLEAN DEFAULT TRUE,
    captcha_type TEXT DEFAULT 'math',        -- 'click' (单选), 'math' (四则算术), 'choice' (图符匹配)
    timeout INTEGER DEFAULT 60,              -- 超时秒数 (默认 60s)
    action_on_fail TEXT DEFAULT 'kick',      -- 'kick' (踢出可再进) 或 'ban' (拉黑)
    auto_delete_prompt BOOLEAN DEFAULT TRUE, -- 完成/超时后是否自动删除验证题
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 4. 待完成验证任务跟踪表 (内存与恢复使用)
CREATE TABLE IF NOT EXISTS pending_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    expected_answer TEXT NOT NULL,
    expire_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, user_id)
);

-- 5. 入群欢迎配置表 (关联消息模板)
CREATE TABLE IF NOT EXISTS group_welcome_settings (
    group_id INTEGER PRIMARY KEY,
    enabled BOOLEAN DEFAULT TRUE,
    active_template_id INTEGER,              -- 当前生效的消息模板 ID
    delete_ttl INTEGER DEFAULT 120,          -- 自动撤回秒数 (0=不撤回, 默认 120 秒)
    clean_previous BOOLEAN DEFAULT TRUE,     -- 进新人时是否自动撤回上一条未销毁欢迎语
    last_welcome_message_id INTEGER DEFAULT NULL,
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE,
    FOREIGN KEY(active_template_id) REFERENCES message_templates(template_id) ON DELETE SET NULL
);

-- 6. 敏感词规则表
CREATE TABLE IF NOT EXISTS group_sensitive_rules (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,                   -- 敏感词或正则表达式
    match_type TEXT DEFAULT 'contains',      -- 'contains', 'exact', 'regex'
    punish_action TEXT DEFAULT 'delete_only',-- 'delete_only', 'mute', 'kick', 'ban'
    mute_duration INTEGER DEFAULT 3600,      -- 禁言时长(秒)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 7. 广告与外链安全策略表
CREATE TABLE IF NOT EXISTS group_moderation_settings (
    group_id INTEGER PRIMARY KEY,
    block_tg_links BOOLEAN DEFAULT TRUE,     -- 拦截 t.me 链接
    block_all_urls BOOLEAN DEFAULT FALSE,    -- 拦截一切外部链接
    block_forwards BOOLEAN DEFAULT FALSE,    -- 拦截频道/他人转发消息
    send_warning_msg BOOLEAN DEFAULT TRUE,   -- 是否发送违规告警
    warning_ttl INTEGER DEFAULT 5,           -- 警告消息自动销毁秒数 (默认 5 秒)
    max_warn_count INTEGER DEFAULT 3,        -- 累计违规封禁阈值
    warn_action TEXT DEFAULT 'kick',         -- 达到阈值处罚: 'kick', 'ban', 'mute'
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 8. 用户违规累计表
CREATE TABLE IF NOT EXISTS user_warn_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    warn_count INTEGER DEFAULT 1,
    last_violation_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, user_id),
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 9. 本地 Ollama AI 配置表
CREATE TABLE IF NOT EXISTS group_llm_settings (
    group_id INTEGER PRIMARY KEY,
    enabled BOOLEAN DEFAULT FALSE,
    base_url TEXT DEFAULT 'http://host.docker.internal:11434',
    model_name TEXT DEFAULT 'qwen2.5:7b',
    system_prompt TEXT DEFAULT '你是一个群组智能管家，性格幽默、有问必答、语气简短有力。',
    trigger_on_mention BOOLEAN DEFAULT TRUE,
    trigger_on_reply BOOLEAN DEFAULT TRUE,
    trigger_keywords TEXT DEFAULT '',        -- 逗号分隔的触发前缀 (如 "ai ,小助手 ")
    context_enabled BOOLEAN DEFAULT TRUE,
    context_mode TEXT DEFAULT 'user_isolated', -- 'shared' (群共享) 或 'user_isolated' (按人隔离)
    context_limit INTEGER DEFAULT 6,         -- 历史记忆保留轮数
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 10. AI 对话记忆存储表
CREATE TABLE IF NOT EXISTS llm_chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,                      -- 'user' 或 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 11. 紧急模式与权限控制表 (支撑面板第 6 大模块)
CREATE TABLE IF NOT EXISTS group_emergency_settings (
    group_id INTEGER PRIMARY KEY,
    lockdown_mode BOOLEAN DEFAULT FALSE,     -- 一键防爆群全员禁言锁群开关
    raid_protection BOOLEAN DEFAULT FALSE,   -- 防拉人爆群自动踢出新入群者
    enable_admin_call BOOLEAN DEFAULT TRUE,  -- 允许群成员使用 /admin 或 @admin 呼叫管理员
    notify_admins_dm BOOLEAN DEFAULT FALSE,  -- 触发 /admin 时是否私聊通知管理员
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);

-- 12. 管理员与系统操作审计日志表 (支撑面板第 7 大模块)
CREATE TABLE IF NOT EXISTS admin_audit_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    operator_id INTEGER NOT NULL,
    operator_name TEXT NOT NULL,
    action_type TEXT NOT NULL,               -- 例如 'update_captcha', 'lockdown_on', 'ban_user'
    detail TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(group_id) REFERENCES group_settings(group_id) ON DELETE CASCADE
);
