# Telegram Digest

一个运行在 Linux 服务器（systemd）或 macOS 主机上的个人 Telegram 信息助手：使用你的 Telegram 用户账号增量读取指定群，定时调用模型提取关键信息，再由单独的 Telegram Bot 私聊推送摘要。

## 功能

- 支持公开群、私有群、supergroup 和 channel
- SQLite 持久化消息、采集 cursor、摘要快照和投递状态，不依赖 Telegram 未读状态
- 离线后按照 message ID 自动补采，并通过 `(chat_id, message_id)` 去重；实时事件不推进 cursor，单条保存失败也会在下一次补采时找回
- 北京时间每天 `08:00 / 11:00 / 14:00 / 17:00 / 20:00 / 23:00` 生成摘要
- 08:00 自动覆盖前一天 23:00 后至当天 08:00 的积压消息
- 提取公告、明确结论、行动项、截止时间、金额、价格、版本号、优惠条件、资源链接、风控和热门未决问题
- 公共群和私有 supergroup 尽可能附带原消息链接
- 不下载图片、视频和文件，只保存文本与媒体元信息
- 单批最多 5000 条；定时触发可连续处理最多 3 批，即最多 15000 条
- 每周执行 90 天数据保留维护，并在实际清理后安全执行 SQLite `VACUUM`
- 支持 profile 复用相似群的 topics、keywords 和 ignore keywords
- 10 MiB × 5 的轮转应用日志

## 安全与隐私

Telegram 群消息会发送到你配置的模型 API 或本机兼容网关。不要为不允许发送给该服务的敏感群启用摘要。

项目默认忽略 `.env`、真实群配置、SQLite、运行日志和 Telethon session。session 文件等同于 Telegram 登录凭证，切勿上传或分享。群消息在模型提示词中被标记为不可信数据，不能更改模型角色或指令。日志不记录完整群消息或密钥。

## 前置条件

- macOS
- Python 3.12+
- Telegram `api_id` 和 `api_hash`：<https://my.telegram.org/apps>
- 通过 `@BotFather` 创建的 Bot token
- Anthropic API key，或本机 Anthropic 兼容网关的 auth token、base URL 和模型名
- 你的 Telegram 数字 user ID

先从自己的 Telegram 账号向新 Bot 发送一次 `/start`，否则 Bot 不能主动私聊你。

## 安装

```bash
cp .env.example .env
cp config/groups.example.yaml config/groups.yaml
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
chmod 600 .env
```

编辑 `.env` 并填写 Telegram 与模型配置。所有命令均使用项目内虚拟环境，避免依赖终端是否已经执行 `source`。

## 本机兼容网关

如果模型由当前 Mac 上的 Anthropic 兼容网关提供，`.env` 使用：

```dotenv
# 注释或删除示例中的 ANTHROPIC_API_KEY
ANTHROPIC_AUTH_TOKEN=你的本机网关token
ANTHROPIC_BASE_URL=http://localhost:8317
ANTHROPIC_MODEL=gpt-5.5
ANTHROPIC_COMPATIBILITY_MODE=true
```

`ANTHROPIC_COMPATIBILITY_MODE=true` 会从第一次请求起直接使用普通流式 JSON 模式，避免已知兼容网关先尝试 adaptive thinking 和 `output_config.format`。结果仍必须通过 Pydantic schema 和来源消息 ID 校验。标准 Anthropic Platform 可保持该值为 `false`，并继续使用高级模式及自动降级。

`~/.claude/settings.json` 中的环境变量只保证提供给 Claude Code，不会自动成为普通进程的环境变量，因此必须将网关配置写入项目 `.env`。本机网关需要在 Telegram Digest 启动前可用，并应配置为随登录自动启动。

## Telegram 登录与群配置

首次登录会交互式要求 Telegram 验证码；启用两步验证时还会要求密码：

```bash
.venv/bin/tg-digest login
```

session 保存到 `data/user.session`。列出当前账号可见的群：

```bash
.venv/bin/tg-digest list-chats
```

将需要关注的群 ID 写入 `config/groups.yaml`。公开群也可以使用不带 `@` 的 username，每个群只能配置 `chat_id` 或 `username` 之一。

尚未建立 cursor 的新群默认只导入最近 200 条消息；之后根据持久化 cursor 增量补采所有新消息。`initial_backfill_messages` 只影响首次导入。

## 配置示例

```yaml
timezone: Asia/Shanghai
initial_backfill_messages: 200

profiles:
  wool:
    topics: [银行开户与绑卡奖励, 推荐邀请与新户奖励, 返现与赠金]
    keywords: [开户奖励, 邀请奖励, 返现, 赠金, 截止, 风控]
    ignore_keywords: []

summary:
  cron: "0 8-23/3 * * *"
  model: claude-opus-5
  effort: high
  direct_summary_max_chars: 80000
  chunk_max_chars: 120000
  max_p2_items: 5
  max_messages_per_run: 5000
  max_batches_per_trigger: 3
  output_repair_attempts: 1  # JSON/schema 错误时额外修复一次
  transient_retry_attempts: 3  # 网关过载、流中断、连接失败时按 5s/15s/45s 退避重试
  max_run_failures: 3       # 连续失败后隔离该快照，避免阻塞后续消息

maintenance:
  enabled: true
  retention_days: 90
  cron: "30 3 * * 0"  # 每周日 03:30，本地时区
  vacuum: true

groups:
  - name: 优惠交流群
    profile: wool
    chat_id: -1001234567890
    priority: high
    keywords: [额外关注词]  # 与 profile 合并并去重

  - name: AI 开发群
    chat_id: -1009876543210
    priority: high
    topics: [Claude, Agent, API, VPS]
    keywords: [发布, 停用, 涨价, 漏洞]
    ignore_keywords: [推广, 广告]
```

程序只采集配置中的群。profile 提供共享配置，群自身列表作为附加项合并，不会覆盖或清空 profile。引用不存在的 profile 会在启动时直接报错。

本地过滤是保守的：有实质文本或链接的消息一般都会进入模型候选，关键词不是入选条件；纯表情、签到、群内完全重复和明确忽略的内容会在本地过滤。跨群相同内容保留各自来源，留到模型合并阶段去重。三小时窗口内的消息都会先入库，不会因为本地过滤而影响 cursor。

### 分群摘要与覆盖

多群消息按群独立分块并提取候选，再合并成最终摘要；单个群的小批次仍可直接摘要。每个块的候选必须通过该块的精确来源校验；P3 和带有任何非法来源的条目会整项拒绝。群友的具体发现、实测、成功/失败反馈、操作条件和踩坑经验也属于候选，不要求是公告或热门讨论，但必须保留适用范围和未确认标记。

合并使用已验证候选并保持其事实字段；不匹配原候选的改写或新增来源不被接受。最终不保留未经过条目来源校验的自由文本 overview，避免被拒绝或超出上限的内容通过概览绕过筛选。遗漏的 P0/P1 和有有效候选但整群被遗漏的最高价值候选会从原候选中补回，不生成填充内容。仅完整事实字段相同的条目自动合并来源，来源重叠或产品相同不代表事实相同。

`max_p2_items` 是所有群合计的 P2 上限，不是每群配额。最终先保留 P0/P1，再优先用 P2 覆盖尚未被代表的有效群，最后按模型排名填满剩余名额；没有有效候选的群不强制出摘要。名额不足时仍遵守全局上限。此流程可防止合并和截断导致有效群整群消失，但不能保证模型在提取阶段识别每一条有价值的信息；保守去重也可能保留措辞不同的相似发现。

`omitted_count` 按本次原始记录中未被最终来源引用的消息数计算，本地过滤只加一次，不把合并时省略的候选数当成消息数。已保存的摘要/投递分片继续按原结果恢复，不因新策略重写或补发历史摘要。

## 手动运行

常驻运行：

```bash
.venv/bin/tg-digest run
```

立即补采并生成一批摘要，然后退出：

```bash
.venv/bin/tg-digest summary-now
```

Bot 命令：

- `/summary`：先补采，再立即总结一批尚未处理的消息
- `/status`：查看尚未总结的消息数量和隔离运行数量
- `/retry-failed`：人工重试最早的隔离摘要快照
- `/start` 或 `/help`：显示帮助

只有 `.env` 中的 `TELEGRAM_OWNER_ID` 可以触发命令。手动命令保持单批语义；只有定时调度会在一个锁内连续处理最多 3 批。每次正点摘要前会先根据所有配置群的 cursor 快照主动补采，再生成摘要，因此 Telegram 长连接短暂重置后也能在下一次定时任务恢复缺失消息。没有新消息时不会发送通知；有新消息但没有关键信息时会发送简短的“无关键更新”摘要。

网关临时错误（过载、`stream disconnected`、超时、连接失败、5xx/429）会在单次模型请求内按 `transient_retry_attempts` 指数退避重试，不会让整批摘要直接失败；重试耗尽的临时错误也不会被误判为网关不支持高级模式。

发送给模型的每条记录只包含来源 ID、本地时间、发送者、回复/转发/媒体信息、正文和正文中未出现的隐藏链接；群名、优先级、topics、keywords 和时区以 `<chat_context>` 每个分块只发送一次。

兼容网关输出不符合 JSON Schema 时，程序默认额外请求一次纯结构修复；修复请求不会重新发送原始 Telegram records，只携带错误输出、校验错误和 schema，修复结果仍经过严格来源 ID 验证。一个摘要快照连续失败达到 `max_run_failures` 后会进入 `quarantined`：其消息保持未总结并继续保存在数据库中，但不会阻塞更新的消息。修复网关或代码后可使用 `/retry-failed` 恢复最早的隔离快照。

LaunchAgent 常驻运行后，请通过 Bot `/summary` 手动触发，不要同时启动第二个 `tg-digest run` 或 `summary-now` 进程；服务锁是进程内锁，同时运行两个进程可能重复处理同一份恢复快照。

## Linux 服务器部署（systemd）

当前生产环境部署在 `dmit-us-la`，项目目录 `/root/Telegram_summary`，以可编辑模式安装在服务器自己的 `.venv` 中，模型网关 `cli-proxy-api` 在同一主机的 Docker 内监听 `127.0.0.1:8317`。服务单元模板位于 `deploy/systemd/tg-digest.service`。

在本机通过 SSH 部署（会先运行 ruff 和 pytest，再同步代码、安装、更新 unit 并重启）：

```bash
./deploy/deploy-remote.sh            # 默认 dmit-us-la:/root/Telegram_summary
./deploy/deploy-remote.sh host /path # 指定其他主机或目录
```

同步会排除 `.env`、`config/groups.yaml`、`data/` 和 `.venv/`，服务器上的密钥、群配置、session、数据库和日志不会被覆盖。

服务器上常用命令：

```bash
systemctl status tg-digest
journalctl -u tg-digest -n 100
tail -f /root/Telegram_summary/data/tg-digest.log
```

## macOS 开机自启动

`deploy/` 包含 LaunchAgent 模板和管理脚本。LaunchAgent 使用：

```text
/usr/bin/caffeinate -i <项目目录>/.venv/bin/tg-digest run
```

这样可阻止系统因空闲进入睡眠；合盖、关机或用户登出仍会停止服务。`RunAtLoad=true` 与 `KeepAlive=true` 会在用户登录后启动并在异常退出后重启。

安装前确认：

1. `.env` 和 `config/groups.yaml` 已完成；
2. `.venv/bin/tg-digest` 已安装；
3. `data/user.session` 已由 `login` 创建；
4. `http://localhost:8317` 网关已自动启动并可访问。

安装并启动：

```bash
./deploy/install-launchd.sh
```

查看状态、调度配置和最近日志：

```bash
./deploy/status-launchd.sh
```

重启：

```bash
launchctl kickstart -k "gui/$UID/com.doublex.telegram-digest"
```

卸载 LaunchAgent：

```bash
./deploy/uninstall-launchd.sh
```

卸载脚本不会删除 `.env`、Telegram session、SQLite、日志或项目代码。

本仓库只生成部署文件；在你主动执行 `install-launchd.sh` 前，不会修改 `~/Library/LaunchAgents`，也不会加载或启动后台服务。

## 日志

分阶段日志 `Digest preparation / extraction / merge / coverage / selection` 记录 run ID、chat ID 和计数，不记录消息正文、候选文本、用户名或 URL。可区分：原始/过滤后消息数、合法候选数、合并保留数、补回数、最终引用项数以及 P2 上限排除数。`Digest has no valid candidates` 表示该群有模型输入但没有合法候选；`Digest coverage ... uncovered=1` 表示有候选但最终受选择限制仍未覆盖。

应用日志默认写入：

```text
data/tg-digest.log
```

默认单文件最大 10 MiB，保留 5 个备份。可在 `.env` 覆盖：

```dotenv
TG_DIGEST_LOG_LEVEL=INFO
TG_DIGEST_LOG_FILE=data/tg-digest.log
TG_DIGEST_LOG_MAX_BYTES=10485760
TG_DIGEST_LOG_BACKUP_COUNT=5
TG_DIGEST_LOG_CONSOLE=true
```

Telethon 和 httpx 的 logger 至少为 WARNING，避免连接重置、更新差异和每个 HTTP 请求的 INFO 日志淹没应用日志。

systemd 单元和 LaunchAgent 都会设置 `TG_DIGEST_LOG_CONSOLE=false`，避免标准输出重复记录完整应用日志。启动级异常单独写入 `data/launchd.stderr.log`。

## 数据保留与恢复语义

每周日 03:30 的维护任务默认：

- 删除超过 90 天且状态为 `delivered` 的摘要运行；
- 删除超过 90 天、已经总结且不再被保留运行引用的源消息；
- 永不删除未总结消息，以及 `prepared`、`generated`、`delivering`、`failed`、`quarantined` 运行或其消息；
- 保留 ingestion cursor，因此维护后仍能继续精确增量采集；
- 在有实际删除时执行 SQLite `VACUUM`。

维护与摘要共用服务锁。摘要正在运行时，本次维护会跳过，不会与投递或数据库写入并发执行。

每次摘要先固定一份消息快照。只有全部 Telegram 分片成功投递后，源消息才标记为已总结。模型结果和已发送的 Telegram message ID 会持续保存。进程重启后优先恢复未完成运行；失败批次不会导致后续消息被跳过。

Telegram Bot 不能读取私聊历史，投递恢复只依赖数据库记录的分片 message ID。极端情况下，如果分片已经发送但进程在数据库回执落盘前退出，该分片仍可能重复发送。

## 开发与测试

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python -m compileall -q src tests
.venv/bin/pip check
```

部署资产可在不加载 LaunchAgent 的情况下验证：

```bash
bash -n deploy/*.sh
plutil -lint deploy/com.doublex.telegram-digest.plist.template
```

## 已知边界

- 普通旧式私有群不一定能生成可点击的单条消息链接。
- Telegram 不提供通用投递幂等键；摘要编号和数据库记录可显著降低但不能完全消除极端网络中断时的重复分片。
- 不解析图片内容、语音、视频或附件正文。
- 定时触发最多处理 `max_messages_per_run × max_batches_per_trigger` 条；更大积压会保留到下一次运行。
