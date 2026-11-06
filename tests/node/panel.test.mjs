import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { get } from 'node:http'
import { startPanel } from '../../dist/panel.js'

test('authenticated live stream follows worker changes and stops cleanly', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'bili-live-'))
  let stage = 'analyzing'
  const bridge = { async call() { return { ready: true, stage } } }
  const panel = await startPanel(bridge, Promise.resolve(''), () => ({ configured: true }), dir)
  const url = new URL(panel.url)
  const controller = new AbortController()
  let closed = false
  try {
    assert.equal((await fetch(`${url.origin}/api/events`)).status, 401)
    const headers = { Authorization: `Bearer ${url.hash.slice(1)}` }
    assert.equal((await fetch(`${url.origin}/api/events`, { headers: { ...headers, Origin: 'https://example.com' } })).status, 403)
    const response = await fetch(`${url.origin}/api/events`, { headers, signal: controller.signal })
    assert.match(response.headers.get('content-type'), /text\/event-stream/)
    const reader = response.body.getReader()
    assert.match(new TextDecoder().decode((await reader.read()).value), /analyzing/)
    stage = 'sent'
    assert.match(new TextDecoder().decode((await reader.read()).value), /sent/)
    await panel.close()
    closed = true
    assert.equal((await reader.read()).done, true)
  } finally { controller.abort(); if (!closed) await panel.close(); await rm(dir, { recursive: true, force: true }) }
})

test('panel requires local authenticated same-origin actions and never exposes arbitrary RPC', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'bili-panel-'))
  const calls = []
  const bridge = { async call(method, params) { calls.push({ method, params }); return { ready: true, pid: 123 } } }
  const panel = await startPanel(bridge, Promise.resolve(''), () => ({ configured: true }), dir)
  const url = new URL(panel.url)
  const headers = { Authorization: `Bearer ${url.hash.slice(1)}`, Origin: url.origin, 'Content-Type': 'application/json' }
  const post = (method, options = {}) => fetch(`${url.origin}/api/action`, { method: 'POST', headers, body: JSON.stringify({ method, params: {} }), ...options })
  try {
    assert.equal(JSON.parse(await readFile(join(dir, 'panel.json'), 'utf8')).url, panel.url)
    const page = await fetch(url.origin)
    assert.match(await page.text(), /<script src="\/panel.js" defer><\/script>/)
    assert.match(page.headers.get('content-security-policy'), /frame-ancestors 'none'/)
    assert.equal((await fetch(`${url.origin}/api/status`)).status, 401)
    const invalidHost = await new Promise((resolve, reject) => {
      get(`${url.origin}/api/status`, { headers: { ...headers, Host: 'untrusted.example' } }, response => {
        response.resume(); response.on('end', () => resolve(response.statusCode))
      }).on('error', reject)
    })
    assert.equal(invalidHost, 403)
    assert.equal((await post('agent.start', { headers: { ...headers, Origin: 'https://example.com' } })).status, 403)
    assert.equal((await post('agent.start', { headers: { ...headers, Authorization: 'Bearer bad' } })).status, 401)
    assert.equal((await post('worker.shutdown')).status, 400)
    assert.equal((await post('agent.start', { body: '{bad' })).status, 400)
    assert.equal((await post('agent.start', { body: JSON.stringify({ method: 'agent.start', params: { value: 'x'.repeat(66000) } }) })).status, 413)
    assert.deepEqual(calls, [])
    const health = await fetch(`${url.origin}/api/status`, { headers })
    assert.equal((await health.json()).pid, 123)
    assert.equal((await post('agent.pause')).status, 200)
    assert.deepEqual(calls.map(call => call.method), ['worker.health', 'agent.pause'])
  } finally { await panel.close(); await assert.rejects(readFile(join(dir, 'panel.json')), { code: 'ENOENT' }); await rm(dir, { recursive: true, force: true }) }
})

test('panel reports startup failure and sanitizes RPC errors', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'bili-panel-error-'))
  const bridge = { async call() { throw new Error('private cookie and request URL must never escape') } }
  let panel = await startPanel(bridge, Promise.resolve('DATA_DIR_IN_USE'), () => ({ configured: false }), dir)
  try {
    const url = new URL(panel.url)
    const response = await fetch(`${url.origin}/api/status`, { headers: { Authorization: `Bearer ${url.hash.slice(1)}` } })
    assert.deepEqual(await response.json(), { ready: false, error: 'DATA_DIR_IN_USE', model_selection: { configured: false } })
    await panel.close()
    panel = await startPanel(bridge, Promise.resolve(''), () => ({ configured: false }), dir)
    const second = new URL(panel.url)
    const headers = { Authorization: `Bearer ${second.hash.slice(1)}`, Origin: second.origin, 'Content-Type': 'application/json' }
    const guarded = await fetch(`${second.origin}/api/action`, { method: 'POST', headers, body: JSON.stringify({ method: 'model.check', params: {} }) })
    assert.equal(guarded.status, 409)
    const failed = await fetch(`${second.origin}/api/status`, { headers })
    assert.deepEqual(await failed.json(), { error: 'PANEL_REQUEST_FAILED' })
  } finally { await panel.close(); await rm(dir, { recursive: true, force: true }) }
})
