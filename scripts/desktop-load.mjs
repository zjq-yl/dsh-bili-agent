// 实际启动隔离的web宿主并确认插件加载；不打开浏览器，不触发模型或B站请求。
import { spawn } from 'node:child_process'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdir, writeFile } from 'node:fs/promises'
import { once } from 'node:events'

const desktop = process.argv[2]
if (!desktop) throw new Error('Pass desktop installation directory')
const root = fileURLToPath(new URL('../', import.meta.url))
const directory = resolve(root, '.desktop-test')
await mkdir(directory, { recursive: true })
const patch = resolve(directory, 'load.patch.yml')
await writeFile(patch, `- insert:
    - id: bili-agent
      name: ${JSON.stringify(resolve(root, 'dist/host.js').replaceAll('\\', '/'))}
      config:
        pythonPath: python
        dataDir: ${JSON.stringify(resolve(directory, 'agent').replaceAll('\\', '/'))}
    - id: bili-tool-probe
      name: ${JSON.stringify(resolve(root, 'scripts/desktop-tool-probe.mjs').replaceAll('\\', '/'))}
`)
const cli = resolve(desktop, 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js')
const child = spawn(resolve(desktop, 'DeepSeek Harness.exe'), ['--expose-internals', cli, 'web', '--patch', patch, '--no-open', '--port', '0'], {
  windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', DSH_HOME: resolve(directory, 'home') },
})
let output = ''
let ready
const started = new Promise(resolve => { ready = resolve })
for (const stream of [child.stdout, child.stderr]) stream.on('data', data => {
  output = (output + data.toString()).slice(-32000)
  if (output.includes('dsh web:')
    && output.includes('[dsh-bili-agent] Tools registered; status tool: bili_agent_status')
    && output.includes('[dsh-bili-agent] Python ready')
    && output.includes('[bili-tool-probe] Agent tools verified')) ready()
  if (output.includes('[bili-tool-probe] FAILED:')) ready()
})
const exited = once(child, 'close')
let timer
try {
  await Promise.race([started, exited.then(() => { throw new Error('Host exited before startup') }),
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Host startup timeout')), 20000) })])
  if (/did not activate|failed to import|bili-agent.*error|\[bili-tool-probe\] FAILED:/i.test(output)) {
    throw new Error(output.replace(/token=[^\s]+/g, 'token=[redacted]'))
  }
  for (const line of output.split('\n')) if (line.startsWith('[bili-tool-probe]')) console.log(line)
  console.log('Desktop isolated web host: real Agent catalogs and status tool verified. No inference or Bilibili call requested.')
} catch (error) {
  console.error(error.message)
  for (const line of output.split('\n')) {
    if (/bili|did not activate|failed to import/i.test(line)) console.error(line.replace(/token=[^\s]+/g, 'token=[redacted]'))
  }
  process.exitCode = 1
} finally {
  clearTimeout(timer)
  if (child.exitCode === null) child.kill()
  await exited
}
