// 使用桌面端随附的真实依赖验证插件；独立临时数据，不读取用户模型密钥。
// ELECTRON_RUN_AS_NODE=1 <DeepSeek Harness.exe> scripts/desktop-smoke.mjs <resources/app.asar/dsh>
import { registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'
import { resolve, join } from 'node:path'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import assert from 'node:assert/strict'

const runtime = process.argv[2]
if (!runtime) throw new Error('Pass desktop resources/app.asar/dsh path')
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@deepseek-ai/') && !context.parentURL?.includes('app.asar')) {
      return nextResolve(specifier, { ...context, parentURL: pathToFileURL(resolve(runtime, 'package.json')).href })
    }
    return nextResolve(specifier, context)
  },
})
const dataDir = await mkdtemp(join(tmpdir(), 'bili-desktop-smoke-'))
let dispose
try {
  const plugin = await import('../dist/host.js')
  const { Context } = await import('@deepseek-ai/cordis')
  const definitions = []
  const ctx = new Context()
  dispose = () => ctx.fiber.dispose()
  ctx.provide('agentDefaultModel', { currentSelection: () => ({ provider: 'smoke', model: 'fake' }) })
  ctx.provide('tools', { register(definition) { definitions.push(definition) } })
  ctx.provide('llm', { async *stream() {
      yield { type: 'text-delta', index: 0, text: '模型连接正常' }
      yield { type: 'finish', reason: { kind: 'stop' } }
  } })
  const config = plugin.Config({ pythonPath: process.env.TEST_PYTHON || 'python', dataDir })
  // Saved configurations from before the panel existed still enable it by default.
  delete config.enablePanel
  delete config.panelPort
  plugin.apply(ctx, config)
  assert.equal(definitions.length, 13)
  const exec = { signal: new AbortController().signal }
  const status = JSON.parse(await definitions.find(d => d.name === 'bili_agent_status').execute({}, exec))
  assert.equal(status.ready, true)
  assert.equal(status.plugin, 'dsh-bili-agent')
  assert.equal(status.tool_namespace, 'bili_agent_')
  assert.equal(status.data_dir, dataDir)
  assert.equal(status.model_configured, true)
  assert.equal(status.model_selection.source, 'host_default')
  assert.equal(status.agent.state, 'PAUSED')
  assert.equal(definitions.some(d => d.name === 'bili_agent_player'), false)
  const dm = JSON.parse(await definitions.find(d => d.name === 'bili_agent_dm').execute({ action: 'status' }, exec))
  assert.equal(dm.send_enabled, false)
  assert.equal(dm.state, 'STOPPED')
  assert.equal(dm.next_poll_at, null)
  assert.ok(Array.isArray(dm.logs))
  assert.ok(Array.isArray(dm.activities))
  const check = JSON.parse(await definitions.find(d => d.name === 'bili_agent_model_check').execute({}, exec))
  assert.equal(check.text, '模型连接正常')
  let panel
  for (let attempt = 0; attempt < 50; attempt++) {
    panel = JSON.parse(await definitions.find(d => d.name === 'bili_agent_status').execute({}, exec)).panel
    if (panel.state !== 'starting') break
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  assert.equal(panel.state, 'ready')
  const panelUrl = new URL(panel.url)
  const headers = { Authorization: `Bearer ${panelUrl.hash.slice(1)}`, Origin: panelUrl.origin, 'Content-Type': 'application/json' }
  const panelHealth = await fetch(`${panelUrl.origin}/api/status`, { headers })
  assert.equal((await panelHealth.json()).pid, status.pid)
  const panelAction = await fetch(`${panelUrl.origin}/api/action`, { method: 'POST', headers, body: JSON.stringify({ method: 'memory.profile', params: {} }) })
  assert.equal((await panelAction.json()).name, '小岚')
  const autoTool = definitions.find(d => d.name === 'bili_agent_automation')
  const auto = JSON.parse(await autoTool.execute({ action: 'status' }, exec))
  assert.deepEqual(auto.slots, ['18:00', '19:00', '20:00'])
  assert.equal(auto.config.dm_auto_start, true)
  assert.equal(auto.model_budget.limit, null)
  assert.equal('model_daily_limit' in auto.config, false)
  const settings = await fetch(`${panelUrl.origin}/api/action`, { method: 'POST', headers,
    body: JSON.stringify({ method: 'automation.configure', params: { config: { start_time: '19:00', end_time: '22:00' } } }) })
  assert.equal(settings.status, 200)
  assert.deepEqual((await settings.json()).slots, ['19:00', '20:00', '21:00'])
  const html = await (await fetch(panelUrl.origin)).text()
  for (const page of ['overview', 'automation', 'videos', 'messages', 'friends-section', 'persona', 'actions']) {
    assert.ok(html.includes(`id="${page}"`), `missing panel page ${page}`)
  }
  await dispose()
  dispose = undefined
  assert.throws(() => process.kill(status.pid, 0), { code: 'ESRCH' })
  await assert.rejects(readFile(join(dataDir, 'panel.json')), { code: 'ENOENT' })
  console.log(JSON.stringify({ desktopDependencies: 'passed', toolCount: definitions.length, pythonWorker: 'passed', panel: 'passed', unload: 'passed', model: 'simulated' }))
} catch (error) {
  console.error(error.stack)
  process.exitCode = 1
} finally {
  if (dispose) await dispose()
  hooks.deregister()
  await rm(dataDir, { recursive: true, force: true })
}
