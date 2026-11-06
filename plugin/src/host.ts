import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import { homedir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WorkerBridge } from './bridge.js'
import type { PluginConfig } from './config.js'
import { generateText, resolveModelConfig } from './model.js'
import { registerTools } from './tools.js'
import { startPanel, type PanelStatus } from './panel.js'

export { Config } from './config.js'
export const name = 'bili-agent'
export const inject = ['tools', 'llm']

/** DSH 插件入口：组装宿主服务、启动 Python 进程，并注册卸载清理。 */
export function apply(ctx: Context, config: PluginConfig) {
  const dshHome = process.env.DSH_HOME || resolve(homedir(), '.dsh')
  const dataDir = resolve(config.dataDir || resolve(dshHome, 'plugins-data/bili-agent'))
  const bridge = new WorkerBridge({
    python: config.pythonPath,
    pythonRoot: fileURLToPath(new URL('../src/', import.meta.url)),
    dataDir,
    settings: {
      asr_model: config.asrModelPath || '',
      dm_send_enabled: config.dmSendEnabled || false,
      dm_allowed_peers: (config.dmAllowedPeers || '').split(/[,，\s]+/).filter(Boolean),
    },
  })
  const modelConfig = () => {
    const defaults = ctx.get('agentDefaultModel') as {
      currentSelection(): { provider: string; model: string }
    } | undefined
    return resolveModelConfig(config, defaults?.currentSelection())
  }
  bridge.handlers.set('model.generate', (params, signal) => generateText(ctx.llm, modelConfig(), params, signal))
  // 就绪失败仍保留诊断工具；不自动切换解释器或回退到其他模型路由。
  const startup = bridge.start().then(
    () => '',
    error => error.code || 'WORKER_START_FAILED',
  )
  ctx.effect(() => () => bridge.close())
  // Older saved plugin configurations may not include the new panel fields yet.
  const panelEnabled = config.enablePanel !== false
  const panel: PanelStatus = { state: panelEnabled ? 'starting' : 'disabled' }
  if (panelEnabled) ctx.effect(async () => {
    try {
      const startupError = await startup
      if (startupError) {
        panel.state = 'failed'; panel.error = startupError
        console.error(`[dsh-bili-agent] Panel unavailable: ${startupError}`)
        return () => {}
      }
      const server = await startPanel(bridge, startup, modelConfig, dataDir, config.panelPort ?? 0)
      panel.state = 'ready'
      panel.url = server.url
      console.info('[dsh-bili-agent] Panel ready; open it with scripts/open-panel.ps1 or the status tool.')
      return async () => { panel.state = 'stopped'; delete panel.url; await server.close() }
    } catch (error) {
      panel.state = 'failed'
      panel.error = (error as NodeJS.ErrnoException).code || 'PANEL_START_FAILED'
      console.error(`[dsh-bili-agent] Panel failed: ${panel.error}`)
      return () => {}
    }
  })
  registerTools(ctx, config, bridge, startup, modelConfig, () => panel)
  console.info('[dsh-bili-agent] Tools registered; status tool: bili_agent_status')
  console.info(`[dsh-bili-agent] Data directory: ${dataDir}`)
  void startup.then(error => {
    if (error) console.error(`[dsh-bili-agent] Python startup failed: ${error}`)
    else console.info('[dsh-bili-agent] Python ready')
  })
}
