import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { WorkerBridge } from './bridge.js'
import type { PluginConfig } from './config.js'
import type { ModelSelection } from './model.js'
import type { PanelStatus } from './panel.js'

// These callbacks only consume cancellation from the host execution context.
type ExecutionContext = { signal: AbortSignal }

const textOutput = {
  schema: { type: 'string' as const },
  render: (_args: unknown, value: string) => [{ type: 'text' as const, text: value }],
}

// 工具只校验调用参数并转发；业务规则与持久化由 Python 核心处理。
export function registerTools(
  ctx: Context,
  config: PluginConfig,
  bridge: WorkerBridge,
  startup: Promise<string>,
  modelConfig: () => ModelSelection,
  panelStatus: () => PanelStatus,
) {
  ctx.tools.register(defineTool({
    name: 'bili_agent_status', description: '检查B站角色插件的Python工作进程与能力状态。',
    parameters: {},
    output: textOutput,
    async execute(_args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ plugin: 'dsh-bili-agent', ready: false, error: startupError, panel: panelStatus() })
      const health = await bridge.call('worker.health', {}, { signal: exec.signal })
      return JSON.stringify({ ...health, plugin: 'dsh-bili-agent', tool_namespace: 'bili_agent_',
        data_dir: bridge.options.dataDir, python_path: bridge.options.python,
        model_configured: modelConfig().configured, model_selection: modelConfig(), panel: panelStatus() })
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_model_check', description: '通过宿主已配置模型执行一次连接测试，会消耗一次模型调用。',
    parameters: {},
    output: textOutput,
    async execute(_args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError || !modelConfig().configured) return JSON.stringify({ error: startupError || 'MODEL_NOT_CONFIGURED' })
      return JSON.stringify(await bridge.call('model.check', {}, { signal: exec.signal, timeoutMs: 70000 }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_enqueue', description: '将带时间戳的字幕JSON加入分析队列。默认暂停；这不是视频播放。',
    parameters: { video_json: { type: 'string', required: true, description: '包含bvid、字符串cid、title、duration和subtitles数组的JSON；每条字幕含from、to、content。' } },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call('agent.enqueue', { video: JSON.parse(args.video_json) }, { signal: exec.signal }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_control', description: '启动、继续、暂停或停止字幕分析循环；start/resume会产生模型调用。start提供keyword时自主搜索最多3轮。retry只重新排队指定失败任务。',
    parameters: {
      action: { type: 'string', required: true, description: 'start、resume、pause、stop、retry之一' },
      video_id: { type: 'number', description: 'retry时必须提供状态列表中的任务ID' },
      keyword: { type: 'string', description: 'start时可提供自主发现主题；resume不提供以保留当前发现进度' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      if (!['start', 'resume', 'pause', 'stop', 'retry'].includes(args.action)) return JSON.stringify({ error: 'INVALID_ACTION' })
      if (['start', 'resume'].includes(args.action) && !modelConfig().configured) return JSON.stringify({ error: 'MODEL_NOT_CONFIGURED' })
      const method = args.action === 'resume' ? 'start' : args.action
      return JSON.stringify(await bridge.call(`agent.${method}`, { video_id: args.video_id, keyword: args.action === 'start' ? args.keyword : undefined }, { signal: exec.signal }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_result', description: '读取任务的字幕总结、证据时间戳和分析限制。',
    parameters: { video_id: { type: 'number', required: true, description: '状态列表中的任务ID' } },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call('agent.result', { video_id: args.video_id }, { signal: exec.signal }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_search', description: '搜索B站视频，返回最多10个候选；不会自动分析或互动。',
    parameters: { keyword: { type: 'string', required: true, description: '搜索词，1至100字' } },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call('platform.search', { keyword: args.keyword }, { signal: exec.signal, timeoutMs: 70000 }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_fetch', description: '获取B站指定分P的字幕并入队；循环运行时会自动分析。无字幕时返回明确错误。',
    parameters: {
      bvid: { type: 'string', required: true, description: '视频BV号' },
      page: { type: 'number', description: '分P序号，从1开始，默认1' },
      language: { type: 'string', description: '首选字幕语言，默认zh-CN' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call('platform.enqueue', args, { signal: exec.signal, timeoutMs: 70000 }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_memory', description: '查看角色设定和兴趣，或用关键词检索已经保存的分析经历。',
    parameters: { query: { type: 'string', description: '省略则查看角色；提供关键词则检索最多5条记忆' } },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call(args.query ? 'memory.recall' : 'memory.profile', args, { signal: exec.signal }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_set_profile', description: '按账号主人要求设置名字、风格、完整人设和选片关键词；省略的可选字段保持原值，不改变发送权限。',
    parameters: {
      name: { type: 'string', required: true, description: '角色名称，最多30字' },
      style: { type: 'string', required: true, description: '角色表达风格，最多1000字' },
      persona: { type: 'string', description: '完整人设提示词，1至12000字；不能授予权限或取消证据要求' },
      preferred_keywords: { type: 'array', items: { type: 'string' }, description: '自主选片优先关键词，最多20个，每个最多50字；空数组清空' },
      avoided_keywords: { type: 'array', items: { type: 'string' }, description: '自主选片标题排除关键词，最多20个，每个最多50字；空数组清空' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call('memory.configure', { profile: args }, { signal: exec.signal }))
    },
  }))
  if (config.enablePlayer) ctx.tools.register(defineTool({
    name: 'bili_agent_player', description: '可选播放器扩展。读取媒体进度、播放区间或保存截图，不属于字幕/音频基础模式。',
    parameters: {
      action: { type: 'string', required: true, description: 'status/open/observe/play/pause/seek/frame/close' },
      bvid: { type: 'string', description: 'open时必填的BV号' },
      page: { type: 'number', description: 'open时分P序号，默认1' },
      seconds: { type: 'number', description: 'seek目标秒数；跳转不增加已播放区间' },
      visible: { type: 'boolean', description: 'open默认不显示浏览器；用户需要登录或查看播放窗口时才设为true' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      return JSON.stringify(await bridge.call('player.command', args, { signal: exec.signal, timeoutMs: 50000 }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_media', description: '后台获取字幕或音频，支持本地语音转写；返回任务ID后查询进度。总结任务加入队列，启动分析循环后生成总结。',
    parameters: {
      action: { type: 'string', required: true, description: 'start/status/cancel' },
      bvid: { type: 'string', description: 'start时的BV号' },
      page: { type: 'number', description: '分P，默认1' },
      mode: { type: 'string', description: 'auto优先字幕，audio只取音频，both取字幕和音频；默认auto' },
      transcribe: { type: 'boolean', description: '是否进行本地音频转写，默认false；需要配置asrModelPath' },
      job_id: { type: 'string', description: 'status查询的任务ID' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      if (!['start', 'status', 'cancel'].includes(args.action)) return JSON.stringify({ error: 'INVALID_ACTION' })
      return JSON.stringify(await bridge.call(`media.${args.action}`, args, { signal: exec.signal }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_friends', description: '查看或设置好友兴趣、学习/休闲偏好与推送节奏，记录对已送达视频的明确反馈。仅按用户要求修改，不能凭推测更改水平或扩大发送权限。',
    parameters: {
      action: { type: 'string', required: true, description: 'status/save/preferences/feedback' },
      profile_json: { type: 'string', description: 'save：peer_uid、name、interests、category、style、recommend、auto_send，可带preferences。preferences动作：{peer_uid,preferences:{mode:learning或leisure,level,goal,max_seconds,daily_limit,push_start,push_end,paused_until,blocked_keywords,blocked_authors}}，省略字段保留。feedback动作：{peer_uid,bvid,kind}，kind为like/too_basic/too_hard/too_long/not_interested/seen/block_author。' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      if (!['status', 'save', 'preferences', 'feedback'].includes(args.action)) return JSON.stringify({ error: 'INVALID_ACTION' })
      const value = args.action === 'status' ? {} : JSON.parse(args.profile_json || '{}')
      const params = args.action === 'save' ? { profile: value } : value
      return JSON.stringify(await bridge.call(`friends.${args.action}`, params, { signal: exec.signal }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_automation', description: '查看或设置开机私信监听、北京时间刷视频时段、每轮数量、三档评价、自动推荐与点赞收藏开关。只按账号主人要求改配置。',
    parameters: {
      action: { type: 'string', required: true, description: 'status/configure/folders' },
      config_json: { type: 'string', description: 'configure的JSON配置：dm_auto_start、scheduled、start_time、end_time、rounds、per_round、request_enabled、actions_enabled、proactive_enabled、share_cards、favorite_folder、max_video_seconds、comments_enabled、comment_daily_limit（0至10，默认3）；省略字段保持原值。视频Agent主动推荐，聊天Agent负责好友点播及公开评论。' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      if (!['status', 'configure', 'folders'].includes(args.action)) return JSON.stringify({ error: 'INVALID_ACTION' })
      return JSON.stringify(await bridge.call(`automation.${args.action}`, args.action === 'configure'
        ? { config: JSON.parse(args.config_json || '{}') } : {}, { signal: exec.signal, timeoutMs: 75000 }))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'bili_agent_dm', description: '管理当前账号的一对一文本私信：读取、草稿、发送、对话总结与隔离记忆。发送受插件配置的开关和收件人范围控制。',
    parameters: {
      action: { type: 'string', required: true, description: 'account/status/sync/history/reply/compose/send/summarize/memory/forget/start/stop' },
      peer_uid: { type: 'string', description: '对方UID，使用字符串；history/compose/summarize/memory/forget必填，sync可选' },
      msg_key: { type: 'string', description: 'reply引用的已收消息ID；只生成草稿' },
      text: { type: 'string', description: 'compose指定的私信正文，最多800字' },
      command_id: { type: 'string', description: 'compose幂等键，8至80位字母数字横线下划线；重试必须复用' },
      draft_id: { type: 'string', description: 'send要发布的草稿ID' },
      auto_send: { type: 'boolean', description: 'start是否自动发送回复；省略时按发送开关和UID白名单自动决定。false明确开启仅生成草稿。' },
    },
    output: textOutput,
    async execute(args, exec: ExecutionContext) {
      const startupError = await startup
      if (startupError) return JSON.stringify({ error: startupError })
      if (!['account', 'status', 'sync', 'history', 'reply', 'compose', 'send', 'summarize', 'memory', 'forget', 'start', 'stop'].includes(args.action)) return JSON.stringify({ error: 'INVALID_ACTION' })
      if (['reply', 'summarize', 'start'].includes(args.action) && !modelConfig().configured) return JSON.stringify({ error: 'MODEL_NOT_CONFIGURED' })
      return JSON.stringify(await bridge.call(`dm.${args.action}`, args, { signal: exec.signal, timeoutMs: args.action === 'sync' ? 600000 : 75000 }))
    },
  }))
}
