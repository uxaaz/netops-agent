# One-click demo for netops-agent: start the zero-dependency HTTP server and open a Q&A page.
# ASCII-only (PowerShell 5.1 reads BOM-less scripts as ANSI); the demo question is read from docs.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$port = 8000

function Test-Port([int]$p) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $p)
        $client.Close()
        return $true
    } catch { return $false }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host '[X] Python not found. Install Python 3.10+ first: https://www.python.org/downloads/'
    exit 1
}

if (Test-Port $port) {
    Write-Host "[*] port $port already serving, reusing the running instance"
} else {
    Write-Host "[*] starting netops-agent on port $port ..."
    Start-Process -FilePath 'python' -ArgumentList @('app\server.py', '--port', "$port") -WorkingDirectory $root -WindowStyle Minimized
}

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Port $port) { $ready = $true; break }
}
if (-not $ready) { Write-Host '[X] service did not become ready in 30s, check the python window'; exit 1 }

# first line of docs/shots-questions.txt is the demo question (stored as UTF-8 there)
$questionFile = Join-Path $root 'docs\shots-questions.txt'
$question = 'SW-CORE-01 status'
if (Test-Path $questionFile) {
    $lines = [System.IO.File]::ReadAllLines($questionFile, [System.Text.Encoding]::UTF8) | Where-Object { $_.Trim() -ne '' }
    if ($lines.Count -gt 0) { $question = $lines[0] }
}
$url = "http://127.0.0.1:$port/?q=" + [System.Uri]::EscapeDataString($question)

Write-Host "[OK] chat page: http://127.0.0.1:$port/"
Write-Host '     demo: ask a threshold question (answer + sources), then a device question (tool call),'
Write-Host '           then an out-of-scope question (refusal)'
Start-Process $url
