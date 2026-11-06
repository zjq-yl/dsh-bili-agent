// Mounted only by desktop-load.mjs in its isolated test home; no model request.
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'

export const inject = ['agents', 'tools', 'agentPresets']

export function apply(ctx) {
  console.log('[bili-tool-probe] Loaded')
  void inspect()

  async function inspect() {
    console.log('[bili-tool-probe] Starting')
    try {
      for (const preset of ['standard', 'minimal', 'ptc']) {
        const handle = await ctx.agents.create({
          sessionId: `bili-tool-probe-${randomUUID()}`,
          agentOptions: { provider: 'unused-probe', model: 'unused-probe' },
          setup: async agentCtx => { await ctx.agentPresets.mount(agentCtx, preset) },
        })
        try {
          const names = ctx.tools.schemas(handle.agent).map(tool => tool.name)
          const biliNames = names.filter(name => name.startsWith('bili_agent_'))
          assert.equal(biliNames.length, 13, `${preset}: missing agent-visible plugin tools`)
          const wire = ctx.tools.wireSchemas(handle.agent).schemas.map(tool => tool.name)
          if (preset !== 'ptc') assert.ok(wire.includes('bili_agent_status'), `${preset}: missing model-facing status tool`)
          const tool = ctx.tools.get('bili_agent_status', handle.agent)
          const status = JSON.parse(await tool.execute({}, { signal: new AbortController().signal }))
          assert.equal(status.ready, true)
          assert.equal(status.plugin, 'dsh-bili-agent')
          assert.equal(status.model_configured, true)
          assert.equal(status.model_selection.source, 'host_default')
          console.log(`[bili-tool-probe] ${preset}: registry=${biliNames.length}, direct=${wire.filter(name => name.startsWith('bili_agent_')).length}, status=${status.ready}`)
        } finally {
          await handle.dispose()
        }
      }
      console.log('[bili-tool-probe] Agent tools verified')
    } catch (error) {
      console.error(`[bili-tool-probe] FAILED: ${error.message}`)
    }
  }
}
