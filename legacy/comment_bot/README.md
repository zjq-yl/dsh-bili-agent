# 旧评论原型（归档）


本目录仅保留旧评论原型及其独立依赖，不是正式插件入口。所有下列命令均在 `legacy/comment_bot/` 目录运行。

Python 负责读取评论、筛选、去重和发送，DSH 负责生成自然的短回复。
默认只打印回复预览；传入 `--send` 才真正发送。

## 已实现

- 单账号、配置指定视频，按时间读取最近几页一级评论。
- 关键词或真实 @ 账号 UID 触发，过滤自己的评论和过期评论。
- 固定人设、视频标题和简介上下文，模型可以选择不回复。
- SQLite 保存预览、发送、失败状态；重启后继续去重。
- 每轮处理上限、发送间隔、北京时间每日发送尝试上限。
- 发送超时或响应缺少评论 ID 时标记 `uncertain`，不会自动重发。
- 同一数据库的进程互斥，防止不小心重复启动。

## 安装与配置

需要 Python 3.11+。DSH SDK 当前文档支持 Windows x64、Linux x64/arm64 和 macOS arm64。

在项目目录运行（PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.toml config.toml
```

编辑 `config.toml`，将 `videos` 中占位符换成自己的 BV 号，调整 `keywords` 和 `persona`。
`keywords = []` 表示仅响应 @。所有相对路径以配置文件目录为基准。
模型名称需与你的 DeepSeek 接口支持的模型匹配。

设置当前终端的环境变量（程序不自动读取 `.env` 文件）：

```powershell
$env:DEEPSEEK_API_KEY = "你的模型密钥"
$env:BILI_SESSDATA = "你的 SESSDATA"
$env:BILI_JCT = "你的 bili_jct"
$env:BILI_BUVID3 = "你的 buvid3"
# 如需兼容接口，可设置 DEEPSEEK_BASE_URL
```

B 站凭据来自本人已登录浏览器中 bilibili.com 的 Cookie；名称区分大小写。
不要把凭据贴入聊天或提交进仓库。启动时会查询实际登录 UID，供自身评论过滤和 @ 检测使用。
预览也需要登录，以支持翻页和识别账号；真实发送额外要求 `BILI_JCT`。

## 运行

先扫描一轮，仅预览（调用模型会产生模型费用）：

```powershell
.\.venv\Scripts\python.exe bot.py --once
```

持续预览：

```powershell
.\.venv\Scripts\python.exe bot.py
```

开启真实回复：

```powershell
.\.venv\Scripts\python.exe bot.py --send
```

按 Ctrl+C 停止。初次运行会处理配置时间窗口内已有的匹配评论（默认最近一小时）。
预览不会消耗真实发送的去重记录；切换到发送模式会重新生成回复，不会直接发布旧预览。
同一评论同一模式处理后不会重复生成，修改人设也不会自动重跑已有记录。

## 记录与失败处理

默认数据库为 `data/bot.sqlite3`，表名 `replies`：

| 状态 | 含义 |
| --- | --- |
| preview | 生成完成，仅预览 |
| skipped | 模型选择不回复 |
| sent | B 站返回新评论 ID |
| uncertain | 请求可能已送达，需在 B 站人工确认 |
| failed | 生成失败或生成时进程中断 |

可以用 SQLite 查看器查看 `text`、`error`、`remote_id`。错误只保存异常类型，避免第三方错误泄露凭据。
`failed` 和 `uncertain` 不自动重试。确需重试时，先确认 B 站没有实际回复，再由维护者删除对应账号、视频、评论和模式的那一条记录；不要清空整个数据库。
每日上限统计所有已发起的请求，包含结果不确定的请求。轮询读取失败时下轮重试；单轮模式结束时请查看读取失败日志。

## 边界与验证

基础版仅覆盖最近配置页数内的一级评论，不保证高流量视频的完整采集；尚未接入楼中楼追问、字幕、长期用户记忆、私信和直播弹幕。
每条评论使用独立 DSH 会话，避免不同用户的信息串入回答。模型只获得视频标题、简介和当前评论。
DSH 会话日志保存在独立 `data/dsh-home`；不要将该目录共享或提交仓库。

DSH 使用 `sdk-minimal` 加文本配置补丁，禁用默认 Shell、文件和工作目录工具。不会向模型开放 B 站发送工具，发送始终由 Python 执行。
SDK 尚在快速变化，升级后应复核 `dsh-text-only.patch.yml` 中插件 ID，并重新进行预览验证。

离线测试（不需要第三方依赖、账号或网络）：

```powershell
python -m unittest -v
```

测试涵盖触发规则、去重、预览转发送、重启恢复、发送结果未知、限频、配置校验和适配器调用参数。
当前开发环境无法访问 PyPI，未安装外部 SDK，未执行真实 DSH 推理或 B 站收发。适配器依据下列文档实现；真实接口和凭据仍需在你的运行环境验证。

- [DSH Python SDK](https://github.com/deepseek-ai/deepseek-harness/tree/master/python/sdk)
- [DSH Python 接入指南](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md)
- [DSH minimal profile 配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/cordis.patch.yml)
- [B 站 Python 库评论接口](https://github.com/inorilzy/bilibili-api/blob/main/docs/modules/comment.md)
