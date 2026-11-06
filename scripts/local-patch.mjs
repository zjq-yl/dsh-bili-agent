import { mkdir, readFile, writeFile, access } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const entry = fileURLToPath(new URL('../dist/host.js', import.meta.url)).replaceAll('\\', '/')
const directory = new URL('../data/', import.meta.url)
const venvPython = fileURLToPath(new URL('../.venv/Scripts/python.exe', import.meta.url)).replaceAll('\\', '/')
let pythonPath = 'python'
try { await access(venvPython); pythonPath = venvPython } catch {}
const useProjectPython = process.argv.includes('--use-project-python')
if (useProjectPython && pythonPath === 'python') throw new Error('Run scripts/setup-local.ps1 first: project Python is missing.')
await mkdir(directory, { recursive: true })
const patch = `- insert:
    - id: bili-agent
      name: ${JSON.stringify(entry)}
      config:
        pythonPath: ${JSON.stringify(pythonPath)}
        dataDir: ${JSON.stringify(fileURLToPath(new URL('agent/', directory)).replaceAll('\\', '/'))}
        provider: ""
        model: ""
        asrModelPath: ""
        dmSendEnabled: false
        dmAllowedPeers: ""
`
try {
  await writeFile(new URL('local.patch.yml', directory), patch, { flag: 'wx' })
  console.log('Created data/local.patch.yml; blank provider/model inherit the DSH default model.')
} catch (error) {
  if (error.code !== 'EEXIST') throw error
  if (useProjectPython) {
    const file = new URL('local.patch.yml', directory)
    const existing = await readFile(file, 'utf8')
    const entries = existing.match(/^[ \t]*pythonPath:[^\r\n]*/gm) || []
    if (entries.length !== 1) throw new Error('Expected exactly one pythonPath in data/local.patch.yml; update it manually.')
    await writeFile(file, existing.replace(/^([ \t]*pythonPath:)[^\r\n]*/m, (_match, key) => `${key} ${JSON.stringify(venvPython)}`))
    console.log('Using project .venv; preserved model, account scope, and other plugin settings.')
  } else {
    console.log('Kept existing data/local.patch.yml. Update pythonPath manually if the interpreter changed.')
  }
}
