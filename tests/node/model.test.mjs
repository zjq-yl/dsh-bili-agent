import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WorkerBridge } from '../../dist/bridge.js'
import { generateText, resolveModelConfig } from '../../dist/model.js'

test('model selection inherits a complete host pair and never mixes partial overrides', () => {
  const host = { provider: 'account', model: 'flash' }
  assert.deepEqual(resolveModelConfig({ provider: '', model: '' }, host),
    { ...host, source: 'host_default', configured: true })
  assert.deepEqual(resolveModelConfig({ provider: 'custom', model: 'custom-model' }, host),
    { provider: 'custom', model: 'custom-model', source: 'plugin', configured: true })
  assert.equal(resolveModelConfig({ provider: 'custom', model: '' }, host).configured, false)
  assert.equal(resolveModelConfig({ provider: '', model: 'custom-model' }, host).configured, false)
  assert.equal(resolveModelConfig({ provider: '', model: '' }).source, 'missing')
  host.model = 'next-model'
  assert.equal(resolveModelConfig({ provider: '', model: '' }, host).model, 'next-model')
})

test('Python -> host streaming model -> Python, usage persistence and cancellation', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'bili-model-'))
  const options = { python: process.env.TEST_PYTHON || 'python', pythonRoot: fileURLToPath(new URL('../../src/', import.meta.url)), dataDir }
  let bridge = new WorkerBridge(options)
  try {
    let request
    bridge.handlers.set('model.generate', (params, signal) => generateText({
      async *stream(input) {
        request = input
        yield { type: 'reasoning-delta', text: 'not part of output' }
        yield { type: 'text-delta', text: '模型连接正常' }
        yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 4, cacheReadTokens: 2, totalTokens: 16 } }
        yield { type: 'finish', reason: { kind: 'stop' } }
      },
    }, { provider: 'test-provider', model: 'test-model' }, params, signal))
    await bridge.start()
    assert.equal((await bridge.call('model.check')).text, '模型连接正常')
    assert.equal(request.provider, 'test-provider')
    assert.equal(request.tools, undefined)
    let calls = (await bridge.call('worker.health')).recent_model_calls
    assert.equal(calls[0].state, 'completed')
    assert.equal(JSON.parse(calls[0].usage).totalTokens, 16)

    let entered
    const ready = new Promise(resolve => { entered = resolve })
    let cancelled
    const aborted = new Promise(resolve => { cancelled = resolve })
    bridge.handlers.set('model.generate', (_params, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => { cancelled(); reject(new Error('cancelled')) }, { once: true })
      entered()
    }))
    const controller = new AbortController()
    const pending = bridge.call('model.check', {}, { signal: controller.signal })
    const rejection = assert.rejects(pending, /CANCELLED/)
    await ready
    controller.abort()
    await rejection
    await aborted
    calls = (await bridge.call('worker.health')).recent_model_calls
    assert.equal(calls[0].state, 'cancelled')
    assert.equal(bridge.pending.size, 0)
    await bridge.close()
    assert.equal(bridge.child.exitCode, 0)
    bridge = new WorkerBridge(options)
    await bridge.start()
    assert.equal((await bridge.call('worker.health')).recent_model_calls.length, 2)
  } finally { await bridge.close(); await rm(dataDir, { recursive: true, force: true }) }
})

test('host rejects truncated/empty/tool responses, forwards abort and enforces deadline', async () => {
  const config = { provider: 'test', model: 'test', timeoutMs: 30 }
  for (const kind of ['max-tokens', 'tool-calls', 'error', 'aborted', '']) {
    await assert.rejects(generateText({ async *stream() {
      yield { type: 'text-delta', text: '{"partial":' }
      if (kind) yield { type: 'finish', reason: { kind } }
    } }, config, { system: '', text: 'test' }, new AbortController().signal), /MODEL_INCOMPLETE/)
  }
  await assert.rejects(generateText({ async *stream({ signal }) {
    await new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }))
  } }, config, { system: '', text: 'test' }, new AbortController().signal), /aborted/)
})

test('same data directory cannot have two workers', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'bili-lock-'))
  const options = { python: process.env.TEST_PYTHON || 'python', pythonRoot: fileURLToPath(new URL('../../src/', import.meta.url)), dataDir }
  const first = new WorkerBridge(options), second = new WorkerBridge(options)
  try {
    await first.start()
    await assert.rejects(second.start(), /DATA_DIR_IN_USE/)
    assert.equal((await first.call('worker.health')).ready, true)
  } finally { await first.close(); await second.close(); await rm(dataDir, { recursive: true, force: true }) }
})
