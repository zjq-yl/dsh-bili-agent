import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test('local launcher selects project Python while preserving existing plugin settings', async () => {
  const root = await mkdtemp(join(tmpdir(), 'bili patch '))
  try {
    await mkdir(join(root, 'scripts'))
    await mkdir(join(root, '.venv', 'Scripts'), { recursive: true })
    await writeFile(join(root, '.venv', 'Scripts', 'python.exe'), '')
    const script = join(root, 'scripts', 'local-patch.mjs')
    await copyFile(new URL('../../scripts/local-patch.mjs', import.meta.url), script)
    const run = (...args) => spawnSync(process.execPath, [script, ...args], { encoding: 'utf8' })
    let result = run('--use-project-python')
    assert.equal(result.status, 0, result.stderr)
    const file = join(root, 'data', 'local.patch.yml')
    const generated = await readFile(file, 'utf8')
    assert.ok(generated.includes(JSON.stringify(join(root, '.venv', 'Scripts', 'python.exe').replaceAll('\\', '/'))))

    const existing = generated.replace(/pythonPath:.*$/m, 'pythonPath: python')
      .replace('provider: ""', 'provider: "my-provider"')
      .replace('model: ""', 'model: "my-model"')
      .replace('dmAllowedPeers: ""', 'dmAllowedPeers: "12345"')
    await writeFile(file, existing)
    result = run()
    assert.equal(result.status, 0, result.stderr)
    assert.equal(await readFile(file, 'utf8'), existing)
    result = run('--use-project-python')
    assert.equal(result.status, 0, result.stderr)
    const updated = await readFile(file, 'utf8')
    assert.equal(updated.replace(/pythonPath:.*$/m, 'pythonPath: python'), existing)

    await writeFile(file, 'unexpected: config\n')
    result = run('--use-project-python')
    assert.notEqual(result.status, 0)
    assert.equal(await readFile(file, 'utf8'), 'unexpected: config\n')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
