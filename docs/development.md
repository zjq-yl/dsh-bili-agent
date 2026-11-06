# 开发约定与验证

## 从哪里开始读

正常使用由 DSH 加载 `plugin/src/host.ts`，它创建 `WorkerBridge`，启动 `python -m bili_agent.worker`。Python 的 `worker.py` 组装业务服务，接收宿主工具发来的 RPC；需要模型时反向调用宿主，最终由 `plugin/src/model.ts` 使用 `ctx.llm`。模型配置与密钥留在 DSH。

```text
DSH
 └─ plugin/src/host.ts
     ├─ config.ts / tools.ts          配置与工具参数
     └─ bridge.ts                    子进程与双向 RPC
         └─ src/bili_agent/worker.py
             ├─ platform.py          B 站请求
             ├─ media.py             字幕、音频、转写任务
             ├─ runner.py            总结分析循环
             ├─ messaging.py         私信、草稿、对话记忆
             └─ memory.py / store.py 角色、经历与持久化
```

`plugin` 不是另一套机器人业务代码。它是 DSH 必需的 TypeScript 接入层；`bili_agent` 是 Python 包。改私信行为看 `messaging.py`，改音频处理看 `media.py`，改视频总结看 `runner.py`，改工具参数看 `tools.ts`。

控制面板静态资源在 `plugin/panel/`，`plugin/src/panel.ts` 只负责本机 HTTP、认证及转发已有 RPC；不另建 worker，不直接调用 B 站或模型。好友档案和推荐规则在 `src/bili_agent/social.py`；视频完成后唤醒处理，SQLite 经历记录作为可恢复的事件来源，发送共用私信队列、额度和去重。

`legacy/comment_bot/` 保留早期评论原型及独立依赖，正式插件不导入它。`dist/` 是构建输出；不要在其中修改代码。两份设计 DOCX 仍留在根目录，修订始终写回现有修订版文件。

## 代码规则

- Python 用 4 空格，TypeScript 用 2 空格；UTF-8、LF、文件结尾换行，由 `.editorconfig` 约定。
- Python 使用 snake_case，类用 PascalCase；TypeScript 使用 camelCase。边界参数保留协议要求的命名，避免无意义转换。
- 一个模块围绕一项职责；不为每个函数建文件，也不为尚未实现的功能创建抽象层。
- 入口负责组装与生命周期，业务模块负责规则；B 站 SDK 只出现在平台适配器及可选播放器边界。
- 新增公开接口补类型注解；注释解释约束或原因。输入校验、发送去重、取消和事务不可因简化而删除。
- 凭据、数据库、媒体和模型文件不进入源码、测试样本或插件包。

`pyproject.toml` 定义标准 src 包布局、Python 版本和 Ruff 规则；`requirements/` 是依赖的唯一清单来源，根 `requirements.txt` 只引用基础清单。可用 `python -m pip install -e '.[dev]'` 安装可编辑开发包，需要音频时加 `audio` extra。原生 DSH 包直接携带 `src/bili_agent/`，不要求先安装 Python 核心包。

```powershell
python -m ruff check src tests scripts
python -m ruff format src tests scripts
```

当前环境没有 Ruff，以上为配置好的开发命令，尚未完成 Ruff 检查。TypeScript 使用 `tsconfig.json` 的严格检查；首次开发先执行：

```powershell
.\scripts\setup-dev.ps1
```

该脚本使用 DSH 自带的 Node 和 pnpm 安装完整 SDK 类型声明及 TypeScript，然后执行 `tsc --noEmit`。后续可运行 `npm run typecheck`。桌面运行时的依赖转发文件不包含完整类型声明，不能代替开发依赖；`desktop-deps.mjs` 会保留已安装的正式依赖。构建只剥离 TypeScript 类型，不能代替类型检查。完整开发依赖已安装，严格类型检查已通过。

## 回归与打包

```powershell
python -m unittest discover -s tests -t .
node scripts/build.mjs
node --test tests/node/*.test.mjs
python scripts/package.py
```

也可通过 `npm test` 执行 Python 回归、构建和 Node 测试。测试导入路径只在 `tests/__init__.py` 设置一次；测试文件不再各自修改 `sys.path`。

桌面端实际依赖与隔离加载：

```powershell
$dshDir = 'D:\app\vscode\dsh'
$dshNode = Join-Path $dshDir 'resources\runtime\primary-runtime\dependencies\node\bin\node.exe'
$env:ELECTRON_RUN_AS_NODE = '1'
& "$dshDir\DeepSeek Harness.exe" scripts/desktop-smoke.mjs "$dshDir\resources\app.asar\dsh"
& $dshNode scripts/desktop-load.mjs $dshDir
```

烟雾测试使用真实 Cordis 生命周期、DSH 工具定义和 Python 子进程，并验证卸载后子进程已退出；模型为测试替身。隔离加载使用项目专用临时 profile，不修改日常桌面配置、不调用真实模型或 B 站；它还通过 `desktop-tool-probe.mjs` 创建 standard/minimal/ptc 三种真实 Agent，分别验证注册目录、模型侧工具列表及状态工具。PTC 的直接工具列表不包含独立的 `bili_agent_*` 名称，它通过 `run_code` 暴露工具 SDK。

`scripts/package.py` 只按 `package.json` 的 `files` 清单打包到 `data/dsh-bili-agent-0.1.0-dev.1.tgz`，不发布。tarball 的宿主安装流程尚未完整验收，当前验证入口是本地补丁加载。改动打包路径后必须重新构建、检查包中入口及 Python 核心。

第一版验收前不提交 Git。后续按功能或修复建立本地 Conventional Commits，提交前运行相关测试；未经要求不推送。
