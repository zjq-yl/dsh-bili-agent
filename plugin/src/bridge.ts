import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { StringDecoder } from 'node:string_decoder'

type Json = any
type Handler = (params: Json, signal: AbortSignal) => Promise<Json>
type Pending = { resolve: (value: Json) => void; reject: (error: Error) => void; cleanup: () => void }
type WorkerOptions = { python: string; pythonRoot: string; dataDir: string; timeoutMs?: number; settings?: Record<string, unknown> }

export class WorkerBridge extends EventEmitter {
  child?: ChildProcessWithoutNullStreams
  pending = new Map<string, Pending>()
  incoming = new Map<string, AbortController>()
  handlers = new Map<string, Handler>()
  counter = 0
  closed = false
  started?: Promise<Json>
  exited?: Promise<void>
  closing?: Promise<void>
  readonly options: WorkerOptions

  constructor(options: WorkerOptions) {
    super()
    this.options = options
  }

  start(): Promise<Json> {
    if (this.started) return this.started
    if (this.closed) return Promise.reject(new Error('Worker is closed'))
    this.started = this.launch()
    return this.started
  }

  private async launch() {
    const env: Record<string, string> = { PYTHONPATH: this.options.pythonRoot, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }
    for (const key of ['PATH', 'Path', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA']) {
      if (process.env[key]) env[key] = process.env[key]!
    }
    const child = this.child = spawn(this.options.python, ['-u', '-m', 'bili_agent.worker', '--data-dir', this.options.dataDir], {
      env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.exited = new Promise(resolve => {
      child.once('close', () => { this.fail(new Error('Worker exited')); resolve() })
    })
    child.once('error', error => this.fail(error))
    child.stdin.on('error', error => this.fail(error))
    // 不向模型转发 stderr；第三方异常可能含敏感信息。
    child.stderr.on('data', () => {})
    let buffer = ''
    const decoder = new StringDecoder('utf8')
    child.stdout.on('data', chunk => {
      buffer += decoder.write(chunk)
      let newline
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        if (Buffer.byteLength(line) > 1024 * 1024) return this.fail(new Error('RPC message too large'))
        try { this.receive(JSON.parse(line)) } catch { this.fail(new Error('Invalid worker protocol')) }
      }
      if (Buffer.byteLength(buffer) > 1024 * 1024) this.fail(new Error('RPC message too large'))
    })
    try {
      const hello = await this.call('worker.hello', { protocol_version: 1, settings: this.options.settings || {} })
      if (hello.protocol_version !== 1) throw new Error('PROTOCOL_MISMATCH')
      return hello
    } catch (error) {
      await this.close()
      throw error
    }
  }

  private send(message: Json) {
    if (this.closed || !this.child?.stdin.writable) throw new Error('Worker is not connected')
    const line = JSON.stringify(message)
    if (Buffer.byteLength(line) > 1024 * 1024) throw new Error('RPC message too large')
    this.child.stdin.write(line + '\n')
  }

  call(method: string, params: Json = {}, options: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<Json> {
    if (this.pending.size >= 32) return Promise.reject(new Error('BUSY'))
    if (options.signal?.aborted) return Promise.reject(new Error('CANCELLED'))
    const id = `ts-${++this.counter}`
    return new Promise((resolve, reject) => {
      const abort = () => {
        try { this.send({ jsonrpc: '2.0', method: 'rpc.cancel', params: { id } }) } catch {}
        settle(new Error('CANCELLED'))
      }
      const cleanup = () => { clearTimeout(timer); options.signal?.removeEventListener('abort', abort) }
      const settle = (error: Error) => { this.pending.delete(id); cleanup(); reject(error) }
      const timer = setTimeout(() => {
        try { this.send({ jsonrpc: '2.0', method: 'rpc.cancel', params: { id } }) } catch {}
        settle(new Error('RPC_TIMEOUT'))
      }, options.timeoutMs ?? this.options.timeoutMs ?? 10000)
      this.pending.set(id, { resolve, reject, cleanup })
      options.signal?.addEventListener('abort', abort, { once: true })
      try { this.send({ jsonrpc: '2.0', id, method, params }) } catch (error) { settle(error as Error) }
    })
  }

  private receive(message: Json) {
    if (!message || message.jsonrpc !== '2.0') throw new Error('Invalid JSON-RPC')
    if (message.method === 'rpc.cancel') {
      this.incoming.get(message.params?.id)?.abort()
    } else if (message.method && message.id !== undefined) {
      void this.handleRequest(message)
    } else if (message.method) {
      this.emit('notification', message.method, message.params)
    } else {
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      pending.cleanup()
      if (message.error) pending.reject(new Error(message.error.message || 'RPC_ERROR'))
      else if ('result' in message) pending.resolve(message.result)
      else pending.reject(new Error('Invalid RPC response'))
    }
  }

  private async handleRequest(message: Json) {
    const controller = new AbortController()
    if (this.incoming.has(message.id) || this.incoming.size >= 8) {
      this.fail(new Error('Invalid worker request concurrency'))
      return
    }
    this.incoming.set(message.id, controller)
    try {
      const handler = this.handlers.get(message.method)
      if (!handler) throw new Error('METHOD_NOT_FOUND')
      const result = await handler(message.params, controller.signal)
      if (!this.closed) this.send({ jsonrpc: '2.0', id: message.id, result })
    } catch {
      if (!this.closed) {
        try { this.send({ jsonrpc: '2.0', id: message.id, error: { code: -32000, message: controller.signal.aborted ? 'CANCELLED' : 'HOST_CALL_FAILED' } }) } catch {}
      }
    } finally { this.incoming.delete(message.id) }
  }

  private fail(error: Error) {
    if (this.closed) return
    this.closed = true
    for (const pending of this.pending.values()) { pending.cleanup(); pending.reject(error) }
    this.pending.clear()
    for (const controller of this.incoming.values()) controller.abort()
    this.child?.stdin.end()
    if (this.child && this.child.exitCode === null) this.child.kill()
    this.emit('workerExit', error.message)
  }

  close(): Promise<void> {
    if (this.closing) return this.closing
    this.closing = this.dispose()
    return this.closing
  }

  private async dispose() {
    if (!this.child) { this.closed = true; return }
    if (!this.closed) {
      try { await this.call('worker.shutdown', {}, { timeoutMs: 2000 }) } catch {}
      this.child.stdin.end()
    }
    const timer = setTimeout(() => { if (this.child?.exitCode === null) this.child.kill() }, 8000)
    try { await this.exited } finally { clearTimeout(timer); this.fail(new Error('Worker closed')) }
  }
}
