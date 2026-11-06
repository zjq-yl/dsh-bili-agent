param([string]$Python = 'python', [switch]$WithAudio, [switch]$WithBrowser)
$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvDir = Join-Path $projectDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
# uv-created virtual environments may not include pip. Bootstrap it locally in
# the target environment, even when that environment is already activated.
& $venvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Output 'Installing pip into the project virtual environment using ensurepip (offline)...'
    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Could not bootstrap pip. This Python installation needs the standard-library ensurepip module and its bundled wheels.' }
}
& $venvPython -m pip --version
if ($LASTEXITCODE -ne 0) { throw 'pip is installed but could not start in the project virtual environment.' }
& $venvPython -m pip install -r (Join-Path $projectDir 'requirements/base.txt') --disable-pip-version-check --retries 1 --timeout 30
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Run pip with -vv to distinguish a network failure from version incompatibility.' }
if ($WithAudio) {
    & $venvPython -m pip install -r (Join-Path $projectDir 'requirements/audio.txt') --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw 'Audio dependencies failed to install.' }
}
$runtimeOptions = @()
if ($WithAudio) { $runtimeOptions += '--audio' }
if (-not $WithBrowser) {
    & $venvPython (Join-Path $PSScriptRoot 'check-runtime.py') @runtimeOptions
    if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed.' }
    Write-Output "Ready. Set plugin pythonPath to: $venvPython"
    return
}
& $venvPython -m pip install -r (Join-Path $projectDir 'requirements/browser.txt') --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Optional browser dependencies failed to install.' }
$previousBrowserPath = $env:PLAYWRIGHT_BROWSERS_PATH
try {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $venvDir 'playwright-browsers'
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium download failed. Python package installation alone is not enough.' }
    & $venvPython (Join-Path $PSScriptRoot 'check-runtime.py') --browser @runtimeOptions
    if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed; see the reported component.' }
} finally {
    if ($null -eq $previousBrowserPath) { Remove-Item Env:PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue }
    else { $env:PLAYWRIGHT_BROWSERS_PATH = $previousBrowserPath }
}
Write-Output "Ready. Set plugin pythonPath to: $venvPython"
