param(
    [string]$DshDir = 'D:\app\vscode\dsh',
    [string]$Profile = 'web'
)
$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$nodeExe = Join-Path $DshDir 'resources\runtime\primary-runtime\dependencies\node\bin\node.exe'
$desktopExe = Join-Path $DshDir 'DeepSeek Harness.exe'
$runtimeDir = Join-Path $DshDir 'resources\app.asar\dsh'
$cliEntry = Join-Path $runtimeDir 'node_modules\@deepseek-ai\dsh-desktop-host\lib\cli.js'
$venvPython = Join-Path $projectDir '.venv\Scripts\python.exe'
$patchFile = Join-Path $projectDir 'data\local.patch.yml'

foreach ($executable in @($nodeExe, $desktopExe, $venvPython)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Missing executable: $executable. Check -DshDir or run scripts/setup-local.ps1 first."
    }
}

$previousElectronMode = $env:ELECTRON_RUN_AS_NODE
try {
    & $nodeExe (Join-Path $PSScriptRoot 'build.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Plugin build failed; host was not started.' }

    $env:ELECTRON_RUN_AS_NODE = '1'
    & $desktopExe (Join-Path $PSScriptRoot 'desktop-deps.mjs') $runtimeDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the desktop runtime dependencies.' }

    & $nodeExe (Join-Path $PSScriptRoot 'local-patch.mjs') --use-project-python
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the local plugin configuration.' }

    Write-Output "Plugin config: $patchFile"
    Write-Output "DSH profile: $Profile (this is separate from the installed desktop window)"
    Write-Output 'Opening DSH in your browser. Keep this terminal open; press Ctrl+C to stop.'
    Write-Output 'In DSH chat, ask it to call bili_agent_status. Configure a chat model first if prompted.'
    Write-Output 'Use a NEW session in standard mode. PTC mode exposes plugin tools through run_code instead of separate tool entries.'
    Write-Output 'Expected response: plugin=dsh-bili-agent, tool_namespace=bili_agent_. bili_status/bili_run belong to a different plugin.'
    # Use a free port and the normal profile; this does not change the desktop installation.
    & $desktopExe --expose-internals $cliEntry $Profile --patch $patchFile --port 0
    if ($LASTEXITCODE -ne 0) { throw 'DSH exited with an error; see the host output above.' }
} finally {
    if ($null -eq $previousElectronMode) {
        Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    } else {
        $env:ELECTRON_RUN_AS_NODE = $previousElectronMode
    }
}
