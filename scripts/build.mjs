// Node 原生剥离可擦除的 TypeScript 类型；不打包或复制宿主依赖。
import { stripTypeScriptTypes } from 'node:module'
import { mkdir, readdir, readFile, writeFile, cp } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const root = new URL('../', import.meta.url)
await mkdir(new URL('dist/', root), { recursive: true })
for (const name of await readdir(new URL('plugin/src/', root))) {
  if (!name.endsWith('.ts')) continue
  const source = new URL(`plugin/src/${name}`, root)
  const code = stripTypeScriptTypes(await readFile(source, 'utf8'), {
    mode: 'strip', sourceUrl: fileURLToPath(source),
  })
  await writeFile(new URL(`dist/${name.replace(/\.ts$/, '.js')}`, root), code)
}
await cp(new URL('plugin/panel/', root), new URL('dist/panel/', root), { recursive: true })
console.log('Built dist/ (syntax transformation; not a TypeScript type check).')
