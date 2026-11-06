// 开发目录接入桌面端自带依赖，无网络安装；请用桌面端的 Electron Node 执行。
import { readFile, mkdir, writeFile, access } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const runtime = process.argv[2]
if (!runtime) throw new Error('Pass desktop resources/app.asar/dsh path')
const targetRoot = new URL('../node_modules/@deepseek-ai/', import.meta.url)
for (const name of ['schemastery', 'dsh-tools']) {
  const original = resolve(runtime, 'node_modules/@deepseek-ai', name)
  const manifest = JSON.parse(await readFile(resolve(original, 'package.json'), 'utf8'))
  const target = new URL(name + '/', targetRoot)
  const metadata = new URL('package.json', target)
  try {
    const existing = JSON.parse(await readFile(metadata, 'utf8'))
    if (!existing.biliDesktopDevelopmentBridge) {
      console.log(`Keeping installed dependency: ${name}`)
      continue
    }
  } catch (error) { if (error.code !== 'ENOENT') throw error }
  const entry = resolve(original, name === 'schemastery' ? 'lib/index.mjs' : 'lib/index.js')
  await access(entry)
  await mkdir(target, { recursive: true })
  await writeFile(metadata, JSON.stringify({ name: manifest.name, version: manifest.version, type: 'module',
    exports: './index.mjs', biliDesktopDevelopmentBridge: true }, null, 2))
  const url = pathToFileURL(entry).href
  await writeFile(new URL('index.mjs', target), `${name === 'schemastery' ? 'export { default }' : 'export *'} from ${JSON.stringify(url)}\n`)
  console.log(`Linked ${manifest.name}@${manifest.version} in ${fileURLToPath(target)}`)
}
