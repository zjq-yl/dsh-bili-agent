import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WorkerBridge } from '../../dist/bridge.js'

test('native bridge drives durable autonomous subtitle queue end-to-end', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'bili-runner-'))
  const options = { python: process.env.TEST_PYTHON || 'python', pythonRoot: fileURLToPath(new URL('../../src/', import.meta.url)), dataDir }
  let bridge = new WorkerBridge(options)
  try {
    bridge.handlers.set('model.generate', async ({ text }) => {
      const input = JSON.parse(text)
      const evidence = input.subtitles ? [input.subtitles[0].id] : input.notes[0].facts[0].evidence
      return { text: JSON.stringify({ summary: '学习方法', facts: [{ text: '作者建议小目标', evidence }],
        reflection: { topic: '知识', impression: '我想试试这种方法。', valence: 1, evidence } }), usage: null }
    })
    await bridge.start()
    const video = JSON.parse(await readFile(new URL('../../fixtures/learning-subtitles.json', import.meta.url), 'utf8'))
    const first = await bridge.call('agent.enqueue', { video })
    const second = await bridge.call('agent.enqueue', { video: { ...video, cid: '2' } })
    assert.equal((await bridge.call('agent.status')).state, 'PAUSED')
    await bridge.call('agent.start')
    const deadline = Date.now() + 5000
    while ((await bridge.call('agent.result', { video_id: second.id })).state !== 'done') {
      assert.ok(Date.now() < deadline, 'queue did not complete')
      await new Promise(resolve => setTimeout(resolve, 20))
    }
    assert.equal((await bridge.call('worker.health')).recent_model_calls.length, 6)
    const result = await bridge.call('agent.result', { video_id: first.id })
    assert.equal(result.result.basis, 'subtitles_only')
    const memories = await bridge.call('memory.recall', { query: '学习方法' })
    assert.equal(memories.length, 2)
    await bridge.call('agent.stop')
    await bridge.close()
    assert.equal(bridge.child.exitCode, 0)
    bridge = new WorkerBridge(options)
    await bridge.start()
    assert.equal((await bridge.call('agent.status')).state, 'PAUSED')
    assert.deepEqual(await bridge.call('agent.result', { video_id: first.id }), result)
  } finally { await bridge.close(); await rm(dataDir, { recursive: true, force: true }) }
})
