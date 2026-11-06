import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WorkerBridge } from '../../dist/bridge.js'

const pythonRoot = fileURLToPath(new URL('../../src/', import.meta.url))

test('real Python worker: handshake, concurrent requests, unknown method, shutdown', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'bili-bridge-'))
  const bridge = new WorkerBridge({ python: process.env.TEST_PYTHON || 'python', pythonRoot, dataDir })
  try {
    const hello = await bridge.start()
    assert.equal(hello.protocol_version, 1)
    assert.ok(hello.pid > 0)
    const health = await Promise.all(Array.from({ length: 5 }, () => bridge.call('worker.health')))
    assert.ok(health.every(h => h.ready && h.pid === hello.pid))
    await assert.rejects(bridge.call('no.such.method'), /未知方法/)
    assert.equal((await bridge.call('worker.health')).ready, true)
    await bridge.close()
    assert.equal(bridge.child.exitCode, 0)
    assert.equal(bridge.pending.size, 0)
    await assert.rejects(bridge.call('worker.health'), /connected/)
  } finally { await bridge.close(); await rm(dataDir, { recursive: true, force: true }) }
})

test('missing interpreter fails promptly and cleanup is idempotent', async () => {
  const bridge = new WorkerBridge({ python: 'bili-python-not-present-123', pythonRoot, dataDir: tmpdir(), timeoutMs: 1000 })
  await assert.rejects(bridge.start())
  await bridge.close()
  await bridge.close()
  assert.equal(bridge.pending.size, 0)
})

test('protocol mismatch and pre-cancelled requests fail closed', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'bili-protocol-'))
  const bridge = new WorkerBridge({ python: process.env.TEST_PYTHON || 'python', pythonRoot, dataDir })
  try {
    await bridge.start()
    await assert.rejects(bridge.call('worker.hello', { protocol_version: 99 }), /PROTOCOL_MISMATCH/)
    await assert.rejects(bridge.call('worker.health', {}, { signal: AbortSignal.abort() }), /CANCELLED/)
  } finally { await bridge.close(); await rm(dataDir, { recursive: true, force: true }) }
})
