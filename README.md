# DSH B 站角色插件（开发中）

Python 实现字幕/音频获取、总结、私信和长期记忆；TypeScript 将这些能力接入 DSH。当前基础版不需要图像识别或浏览器。

**主代码在 `src/bili_agent/`，Python 进程入口是 `worker.py`。DSH 插件入口是 `plugin/src/host.ts`。** `bili_agent` 是 Python 包名；`plugin` 是宿主接入目录，两者通过标准输入输出上的 JSON-RPC 通信。

## 目录与入口

```text
src/bili_agent/         Python 业务核心
  worker.py            进程入口、服务组装、RPC 分发
  runner.py            分析队列、分段总结、自主循环
  platform.py          B 站 API、字幕、音频地址、私信接口
  media.py             音频下载、后台任务、转写调度
  transcribe.py        本地语音转文字子进程
  messaging.py         私信收取、草稿、发送、对话记忆
  social.py            好友分类、兴趣档案、视频完成后的推荐
  memory.py            视频经历、角色设定、兴趣积累
  store.py             SQLite、模型调用统计、运行日志、进程互斥
  content.py           内容校验、分段、总结证据校验
  model.py             Python 侧宿主模型调用
  protocol.py          双向 RPC、取消和超时
  player.py            默认关闭的旧播放器扩展
plugin/src/            DSH 原生接入
  host.ts              插件启动、卸载与依赖组装
  config.ts            插件配置及其类型
  tools.ts             13 个默认工具的声明和注册
  panel.ts             本地面板服务，共用宿主工作进程
  bridge.ts            Python 子进程与 RPC 连接
  model.ts             调用 DSH 已配置的模型
tests/python/          Python 单元及业务回归测试
plugin/panel/          控制面板 HTML、CSS、JavaScript
tests/node/            Node 模型流、跨进程集成测试
requirements/          基础、音频、浏览器、开发依赖
scripts/               安装、检查、构建和打包命令
docs/                  配置、使用和开发说明
legacy/comment_bot/    旧评论原型；不属于正式插件
fixtures/              明确标记为合成数据的测试样本
dist/                  自动构建产物，不手工修改
data/                  本地配置、数据库、缓存和插件包，不提交
```

旧 `python/` 目录现仅残留被忽略的字节码缓存，清理命令受执行策略限制而保留；运行和打包均不再读取它。

## 本轮已实现

| 能力 | 当前行为 |
| --- | --- |
| 字幕与音频 | 指定 BV 和分 P 获取字幕；下载独立音频轨；可选本地 ASR，保留时间戳与来源 |
| 视频总结 | 分段分析、分层汇总、个人感受、证据引用；校验失败最多纠错一次并显示原因；重启保留检查点 |
| 私信 | 验证登录账号、同步文本消息、生成回复草稿、指定对象发送、定时监听 |
| 评论区提及 | 检查互关好友的新 @通知、理解公开上下文、按需分析视频并回复原楼层；进度可见、持久去重、不确定发送不重试 |
| 好友与推荐 | 真实互关好友/粉丝/关注分类、UID检索、选择回关或不回关；发送前核实关系，未互关先添加；独立推荐偏好、反馈、每日数量与推送时段 |
| 自动运行 | 默认开机监听私信；北京时间18:00–21:00分3轮、每轮目标5个视频；点播保存临时需求、优先处理并复用总结，临时模型故障保留请求继续处理 |
| 长期记忆 | SQLite 保存视频经历及分账号/对象隔离的对话摘要；支持检索和私信记忆遗忘 |
| 角色连续性 | 可编辑完整人设、选片偏好与避开关键词、同一对象历史上下文、缓慢积累兴趣 |
| DSH 接入 | 宿主模型调用、工具注册、子进程生命周期、取消、额度和错误状态 |

这些是已实现并经离线测试验证的能力。**真实 B 站收发、真实模型和本地 ASR 模型推理尚未联调通过，第一版尚未验收完成。** 当前不进行图像识别；旧评论原型尚未并入插件。

## 开始使用

```powershell
# 基础安装：字幕、音频下载和私信；不安装 Chromium
.\scripts\setup-local.ps1
# 需要音频转文字时额外安装
.\scripts\setup-local.ps1 -WithAudio
```

然后按[配置与使用](docs/usage.md)加载到你的 DSH 桌面端，填写本地账号凭据、宿主模型路由，以及可选的 ASR 模型目录。脚本会复用 `.venv`；已有配置文件不会被覆盖。

依赖安装好后，直接打开加载了插件的 DSH 页面：

```powershell
.\scripts\start-local.ps1
```

启动脚本使用 DSH 自带的 Node，无需额外配置 PATH；自动构建、连接宿主依赖、将插件指向项目 `.venv`，然后打开浏览器。保持终端运行，在 DSH 聊天中说“请调用 bili_agent_status 检查插件状态”。

插件默认启动本地控制面板，与 DSH 共用当前 worker。更新代码后先重新加载 DSH 插件，再运行 `.\scripts\open-panel.ps1` 打开面板。可查看状态、验证账号和模型、添加视频、控制分析与重试、查看总结、启动私信监听、发送草稿和设置角色。使用其他数据目录时传入 `-DataDir`；也可从 `bili_agent_status` 返回的 `panel.url` 打开。关闭面板不停止后台任务，卸载插件才会关闭面板服务。

本机基础与音频依赖已安装，运行检查识别到 bilibili-api-python 17.4.2、aiohttp 3.14.5、faster-whisper 1.2.1。ASR 模型文件仍需另外准备；Playwright 已移出基础依赖。

## 开发与验证

首次开发执行 `.\scripts\setup-dev.ps1`，安装完整的 DSH SDK 类型声明并运行严格 TypeScript 检查；不需要将 Node 加入 PATH。

```powershell
python -m unittest discover -s tests -t .
node scripts/build.mjs
node --test tests/node/*.test.mjs
python scripts/package.py
```

未将 Node 加入 PATH 时，使用 DSH 随附的 `D:\app\vscode\dsh\resources\runtime\primary-runtime\dependencies\node\bin\node.exe`。格式规范、桌面端验证和模块边界见[开发说明](docs/development.md)。

验证覆盖 Python 业务、Node RPC、模型流、分析队列、面板访问控制、定时探索、三档评价、推荐请求与启动配置。桌面端真实依赖烟雾测试检查13个工具、面板分区和自动运行配置、与工作进程共享及卸载清理，模型为测试替身。TypeScript 严格检查已通过；构建本身只做语法转换。未把离线测试记作线上验收。

设计文件仍为根目录的 `B站自主浏览智能体技术方案_Python优先修订版.docx`，初版设计保留作参考。第一版完成前不创建 Git 提交。
