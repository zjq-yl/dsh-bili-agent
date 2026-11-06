param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[1-9][0-9]{0,29}$')][string]$PeerUid,
    [string]$DshDir = 'D:\app\vscode\dsh',
    [switch]$StopDsh
)
$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$desktopExe = Join-Path $DshDir 'DeepSeek Harness.exe'
$nodeExe = Join-Path $DshDir 'resources\runtime\primary-runtime\dependencies\node\bin\node.exe'
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
foreach ($executable in @($desktopExe, $nodeExe, $pythonExe)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Missing runtime: $executable" }
}
$desktopProcesses = @(Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue)
if ($desktopProcesses.Count -gt 0 -and $StopDsh) {
    $expectedExecutable = (Resolve-Path -LiteralPath $desktopExe).Path
    # Validate every target before stopping any process. Never stop all Electron/Python processes.
    foreach ($desktopProcess in $desktopProcesses) {
        if (-not $desktopProcess.Path -or $desktopProcess.Path -ine $expectedExecutable) {
            throw "Cannot verify DSH process $($desktopProcess.Id) belongs to this installation. No process was stopped."
        }
    }
    Write-Host "Stopping DSH processes from $expectedExecutable (unsaved DSH work may be lost)."
    foreach ($desktopProcess in $desktopProcesses) {
        if (-not $desktopProcess.HasExited) {
            Stop-Process -InputObject $desktopProcess -ErrorAction Stop
        }
    }
}
if ($desktopProcesses.Count -gt 0) {
    Write-Host 'Waiting up to 10 seconds for DSH to exit...'
    $exitDeadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $desktopProcesses = @(Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue)
        if ($desktopProcesses.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $exitDeadline)
}
if ($desktopProcesses.Count -gt 0) {
    $desktopProcesses | Select-Object Id, MainWindowTitle, Path | Format-Table -AutoSize | Out-Host
    Write-Host 'Closing the window can leave DSH background processes running.'
    Write-Host 'After saving your work, rerun this command with -StopDsh to stop this installation explicitly.'
    throw 'DSH_STILL_RUNNING: configuration was not changed. See the process list above.'
}
$dshDirectory = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
$profileFile = Join-Path $dshDirectory 'profiles\desktop\cordis.patch.yml'
& $nodeExe (Join-Path $PSScriptRoot 'build.mjs')
if ($LASTEXITCODE -ne 0) { throw 'Build failed; configuration was not changed.' }
$update = & $pythonExe (Join-Path $PSScriptRoot 'configure-desktop.py') --profile $profileFile --peer-uid $PeerUid --apply
if ($LASTEXITCODE -ne 0) { throw 'Configuration update failed; DSH was not started.' }
$dataDirectory = ($update | ConvertFrom-Json).settings.data_dir
# A normal interactive desktop launch; do not inherit Electron's CLI mode.
$previousElectronMode = $env:ELECTRON_RUN_AS_NODE
try {
    Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    Start-Process -FilePath $desktopExe
} finally {
    if ($null -ne $previousElectronMode) { $env:ELECTRON_RUN_AS_NODE = $previousElectronMode }
}
Write-Output 'Started DSH. Waiting for the plugin panel. No message has been composed or sent by this script.'
& (Join-Path $PSScriptRoot 'open-panel.ps1') -DataDir $dataDirectory.Trim() -WaitSeconds 60
