import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { mkdir, readFile, writeFile, unlink } from 'node:fs/promises'
import { join } from 'node:path'
import type { WorkerBridge } from './bridge.js'
import type { ModelSelection } from './model.js'

export type PanelStatus = { state: 'disabled' | 'starting' | 'ready' | 'failed' | 'stopped'; url?: string; error?: string }

const methods = new Set([
  'model.check', 'platform.enqueue', 'agent.start', 'agent.pause', 'agent.stop', 'agent.retry', 'agent.result', 'agent.reanalyze', 'library.list', 'library.refresh',
  'memory.profile', 'memory.configure', 'memory.recall',
  'dm.account', 'dm.status', 'dm.sync', 'dm.history', 'dm.reply', 'dm.compose', 'dm.send',
  'dm.start', 'dm.stop', 'dm.memory', 'dm.summarize',
  'friends.status', 'friends.save', 'friends.preferences', 'friends.feedback', 'friends.connect', 'friends.connection',
  'friends.directory', 'friends.lookup', 'friends.contact', 'friends.message',
  'automation.status', 'automation.configure', 'automation.folders', 'automation.retry_review',
])
const usesModel = new Set(['model.check', 'agent.start', 'dm.reply', 'dm.start', 'dm.summarize', 'automation.retry_review'])
const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/panel.js', ['panel.js', 'text/javascript; charset=utf-8']],
  ['/panel.css', ['panel.css', 'text/css; charset=utf-8']],
])

/** Local panel shares the host's worker; it never starts a second account or model client. */
export async function startPanel(
  bridge: Pick<WorkerBridge, 'call'>,
  startup: Promise<string>,
  modelConfig: () => ModelSelection,
  dataDir: string,
  port = 0,
) {
  const token = randomBytes(32).toString('hex')
  const tokenBytes = Buffer.from(`Bearer ${token}`)
  const files = new Map<string, { body: Buffer; type: string }>()
  for (const [route, [name, type]] of assets) {
    files.set(route, { body: await readFile(new URL(`./panel/${name}`, import.meta.url)), type })
  }
  let origin = ''
  const controllers = new Set<AbortController>()
  const streams = new Set<ServerResponse>()
  let streaming = false
  const streamController = new AbortController()
  const safeError = (error: unknown) => error instanceof Error && /^[A-Z][A-Z0-9_]{1,80}$/.test(error.message)
    ? error.message : 'PANEL_REQUEST_FAILED'
  async function snapshot(signal: AbortSignal) {
    const error = await startup
    const model = modelConfig()
    return error ? { ready: false, error, model_selection: model }
      : { ...await bridge.call('worker.health', {}, { signal }), model_selection: model }
  }
  async function broadcast() {
    if (streaming || !streams.size || streamController.signal.aborted) return
    streaming = true
    try {
      let state
      try { state = await snapshot(streamController.signal) }
      catch (error) { state = { ready: false, error: safeError(error) } }
      const frame = `data: ${JSON.stringify(state)}\n\n`
      for (const response of streams) {
        // Disconnect slow readers instead of accumulating unbounded snapshots.
        if (response.writableLength > 1024 * 1024) response.destroy()
        else response.write(frame)
      }
    } finally { streaming = false }
  }
  const streamTimer = setInterval(() => { void broadcast() }, 1000)
  streamTimer.unref()
  function json(res: ServerResponse, code: number, data: unknown) {
    res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' })
    res.end(JSON.stringify(data))
  }
  async function handle(req: IncomingMessage, res: ServerResponse) {
    res.setHeader('cache-control', 'no-store')
    res.setHeader('x-content-type-options', 'nosniff')
    res.setHeader('referrer-policy', 'no-referrer')
    res.setHeader('content-security-policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' https://*.hdslb.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if (req.headers.host !== new URL(origin).host) return json(res, 403, { error: 'INVALID_HOST' })
    const path = new URL(req.url || '/', origin).pathname
    const asset = files.get(path)
    if (req.method === 'GET' && asset) {
      res.writeHead(200, { 'content-type': asset.type })
      res.end(asset.body)
      return
    }
    if (req.headers.origin && req.headers.origin !== origin) return json(res, 403, { error: 'INVALID_ORIGIN' })
    const supplied = Buffer.from(req.headers.authorization || '')
    if (supplied.length !== tokenBytes.length || !timingSafeEqual(supplied, tokenBytes)) {
      return json(res, 401, { error: 'PANEL_AUTH_REQUIRED' })
    }
    const controller = new AbortController()
    controllers.add(controller)
    const cancel = () => { if (!res.writableEnded) controller.abort() }
    res.once('close', cancel)
    try {
      const startupError = await startup
      if (req.method === 'GET' && path === '/api/events') {
        if (streams.size >= 8) return json(res, 429, { error: 'PANEL_TOO_MANY_STREAMS' })
        res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', connection: 'keep-alive' })
        res.flushHeaders()
        streams.add(res)
        void broadcast()
        await new Promise<void>(resolve => {
          if (res.destroyed) return resolve()
          res.once('close', resolve)
        })
        streams.delete(res)
        return
      }
      if (req.method === 'GET' && path === '/api/status') {
        const model = modelConfig()
        return json(res, 200, startupError
          ? { ready: false, error: startupError, model_selection: model }
          : { ...await bridge.call('worker.health', {}, { signal: controller.signal }), model_selection: model })
      }
      if (req.method !== 'POST' || path !== '/api/action') return json(res, 404, { error: 'NOT_FOUND' })
      if (req.headers.origin !== origin) return json(res, 403, { error: 'INVALID_ORIGIN' })
      if (req.headers['content-type']?.split(';')[0] !== 'application/json') return json(res, 415, { error: 'JSON_REQUIRED' })
      if (startupError) return json(res, 503, { error: startupError })
      let size = 0
      const chunks: Buffer[] = []
      for await (const chunk of req) {
        size += chunk.length
        if (size > 65536) { json(res, 413, { error: 'REQUEST_TOO_LARGE' }); return }
        chunks.push(chunk)
      }
      let input: { method?: unknown; params?: unknown }
      try { input = JSON.parse(Buffer.concat(chunks).toString('utf8')) }
      catch { return json(res, 400, { error: 'INVALID_JSON' }) }
      if (!input || typeof input.method !== 'string' || !methods.has(input.method)
          || !input.params || typeof input.params !== 'object' || Array.isArray(input.params)) {
        return json(res, 400, { error: 'INVALID_ACTION' })
      }
      if (usesModel.has(input.method) && !modelConfig().configured) return json(res, 409, { error: 'MODEL_NOT_CONFIGURED' })
      const result = await bridge.call(input.method, input.params, {
        signal: controller.signal, timeoutMs: input.method === 'dm.sync' ? 600000 : 75000,
      })
      json(res, 200, result)
    } catch (error) {
      // Never send arbitrary exception text, paths, request URLs or credentials to the browser.
      const message = error instanceof Error && /^[A-Z][A-Z0-9_]{1,80}$/.test(error.message)
        ? error.message : 'PANEL_REQUEST_FAILED'
      if (!res.headersSent && !res.destroyed) json(res, 502, { error: message })
    } finally {
      controllers.delete(controller)
      res.off('close', cancel)
    }
  }
  const server = createServer((req, res) => {
    void handle(req, res).catch(() => { if (!res.headersSent) json(res, 500, { error: 'PANEL_REQUEST_FAILED' }); else res.destroy() })
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', () => { server.off('error', reject); resolve() })
  })
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('PANEL_LISTEN_FAILED')
  origin = `http://127.0.0.1:${address.port}`
  const url = `${origin}/#${token}`
  const addressFile = join(dataDir, 'panel.json')
  const closeServer = async () => {
    clearInterval(streamTimer)
    streamController.abort()
    for (const response of streams) response.end()
    streams.clear()
    for (const controller of controllers) controller.abort()
    const closed = new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
    server.closeAllConnections()
    await closed
  }
  try {
    await mkdir(dataDir, { recursive: true })
    await writeFile(addressFile, JSON.stringify({ url, pid: process.pid }), { mode: 0o600 })
  } catch (error) { await closeServer(); throw error }
  return {
    url,
    async close() {
      await closeServer()
      try {
        if (JSON.parse(await readFile(addressFile, 'utf8')).url === url) await unlink(addressFile)
      } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error }
    },
  }
}
