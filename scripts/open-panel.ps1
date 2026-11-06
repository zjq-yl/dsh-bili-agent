param(
    [string]$DataDir = '',
    [ValidateRange(0, 60)][int]$WaitSeconds = 10,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ($DataDir) {
    $directories = @([System.IO.Path]::GetFullPath($DataDir))
} else {
    $dshDirectory = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
    $directories = @((Join-Path $projectDir 'data\agent'), (Join-Path $dshDirectory 'plugins-data\bili-agent'))
}
$addressFile = $null
$readyAddress = $null
$lastError = 'PANEL_ADDRESS_NOT_FOUND'
$waitingPrinted = $false
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
do {
    foreach ($directory in $directories) {
        $candidate = Join-Path $directory 'panel.json'
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $addressFile = $candidate
        # A forced restart can leave the old address until the new server has bound.
        # Read it again on every attempt; do not keep probing a stale port/token.
        try {
            $address = Get-Content -LiteralPath $candidate -Raw -Encoding UTF8 | ConvertFrom-Json
            $panelUri = [uri]$address.url
            if ($panelUri.Scheme -ne 'http' -or $panelUri.Host -ne '127.0.0.1' -or $panelUri.Port -le 0 -or
                $panelUri.AbsolutePath -ne '/' -or $panelUri.UserInfo -or $panelUri.Query -or
                $panelUri.Fragment -notmatch '^#[a-f0-9]{64}$') {
                $lastError = 'INVALID_PANEL_ADDRESS'
                continue
            }
        } catch {
            $lastError = 'PANEL_ADDRESS_NOT_READABLE'
            continue
        }
        try {
            $remaining = [Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalSeconds)
            $timeout = [int][Math]::Max(1, [Math]::Min(5, $remaining))
            $response = Invoke-WebRequest -Uri ($panelUri.GetLeftPart([System.UriPartial]::Authority) + '/api/status') -Headers @{ Authorization = 'Bearer ' + $panelUri.Fragment.Substring(1) } -UseBasicParsing -MaximumRedirection 0 -TimeoutSec $timeout
            $status = $response.Content | ConvertFrom-Json
            if ($status.ready -eq $true) {
                $readyAddress = $panelUri
                break
            }
            $lastError = if ($status.error -is [string] -and $status.error -cmatch '^[A-Z][A-Z0-9_]{1,80}$') { $status.error } else { 'WORKER_NOT_READY' }
        } catch {
            # Do not print raw exception text: it may include the authenticated URL.
            $lastError = 'PANEL_STATUS_REQUEST_FAILED'
            if ($_.Exception.Response) {
                $lastError = 'PANEL_HTTP_' + [int]$_.Exception.Response.StatusCode
            } elseif ($_.Exception -is [System.Net.WebException]) {
                $lastError = 'PANEL_NETWORK_' + $_.Exception.Status.ToString().ToUpperInvariant()
            }
        }
    }
    if ($readyAddress -or [DateTime]::UtcNow -ge $deadline) { break }
    if (-not $waitingPrinted) {
        Write-Host 'Waiting for a ready panel; retrying the latest address until the timeout...'
        $waitingPrinted = $true
    }
    Start-Sleep -Milliseconds 250
} while ($true)
if (-not $readyAddress) {
    Write-Host 'No ready panel was found in:'
    $directories | ForEach-Object { Write-Host "  $_" }
    Write-Host "Last health check: $lastError"
    Write-Host 'This command opens an existing panel; it does not start or restart DSH.'
    Write-Host 'Reload the plugin in DSH. If reload keeps the old module, fully exit DSH (including its tray icon) and reopen it.'
    Write-Host 'Then call bili_agent_status: missing panel = old plugin; disabled = enablePanel is false; failed = inspect panel.error.'
    Write-Host 'For a custom data directory, rerun with -DataDir using data_dir from bili_agent_status.'
    if (-not $addressFile) { throw 'PANEL_ADDRESS_NOT_FOUND' }
    throw 'PANEL_NOT_READY'
}
Write-Output "Panel is ready. Data directory: $(Split-Path -Parent $addressFile)"
if ($CheckOnly) { return }
Start-Process -FilePath $readyAddress.AbsoluteUri
Write-Output 'Opened the Bilibili agent panel in your browser.'
