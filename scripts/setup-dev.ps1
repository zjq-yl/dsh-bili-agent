param([string]$DshDir = 'D:\app\vscode\dsh')
$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtime = Join-Path $DshDir 'resources\runtime\primary-runtime\dependencies'
$nodeExe = Join-Path $runtime 'node\bin\node.exe'
$pnpmEntry = Join-Path $runtime 'pnpm\bin\pnpm.mjs'
foreach ($file in @($nodeExe, $pnpmEntry)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Missing DSH development runtime: $file. Check -DshDir."
    }
}

Push-Location -LiteralPath $projectDir
try {
    # pnpm 11 treats --config.fetch-timeout as a string; Undici requires a number.
    # Use pnpm's typed defaults instead of overriding request settings here.
    & $nodeExe $pnpmEntry install --ignore-scripts --store-dir (Join-Path $projectDir 'data/pnpm-store')
    if ($LASTEXITCODE -ne 0) { throw 'Development dependencies failed to install; TypeScript declarations are not ready.' }
    & $nodeExe (Join-Path $projectDir 'node_modules/typescript/bin/tsc') --noEmit
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript check failed. See the diagnostics above.' }
    Write-Output 'Development dependencies installed; strict TypeScript check passed.'
} finally {
    Pop-Location
}
