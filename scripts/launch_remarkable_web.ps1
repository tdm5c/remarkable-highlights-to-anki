param(
    [int]$Port = 8787,
    [switch]$KeepExports
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$BrowserUrl = "http://127.0.0.1:$Port"
$CleanExportArg = if ($KeepExports) { "--keep-exports" } else { "--clean-exports-after-success" }
$env:RM_CLEAN_EXPORTS_AFTER_SUCCESS = if ($KeepExports) { "0" } else { "1" }
$PreferredBrowsers = @(
    $env:RM_WEB_BROWSER,
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { $_ }

function Test-ServerReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $BrowserUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

$networkListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -eq "0.0.0.0" }

if (-not $networkListener) {
    Start-Process `
        -FilePath "python" `
        -ArgumentList @(".\scripts\remarkable_web.py", "--host", "0.0.0.0", "--port", "$Port", $CleanExportArg) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden
}

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if (Test-ServerReady) {
        break
    }
    Start-Sleep -Milliseconds 400
}

$browser = $PreferredBrowsers | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($browser) {
    Start-Process -FilePath $browser -ArgumentList $BrowserUrl
} else {
    Start-Process $BrowserUrl
}
