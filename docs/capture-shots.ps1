# Capture three agent Q&A screenshots using headless Edge.
# Questions are read from shots-questions.txt as UTF-8 (keeps this file ASCII-only).
# Paths derive from $PSScriptRoot so this script stays ASCII-only under PowerShell 5.1.
$ErrorActionPreference = 'Continue'
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$root = $PSScriptRoot
$questions = [System.IO.File]::ReadAllLines((Join-Path $root 'shots-questions.txt'), [System.Text.Encoding]::UTF8) | Where-Object { $_.Trim() -ne '' }
$names = @('chat.png', 'chat-tool.png', 'chat-refuse.png')
if ($questions.Count -ne $names.Count) { throw "expected $($names.Count) questions, got $($questions.Count)" }

for ($i = 0; $i -lt $names.Count; $i++) {
    $q = [System.Uri]::EscapeDataString($questions[$i])
    $url = "http://127.0.0.1:8000/?q=$q"
    $out = Join-Path $root $names[$i]
    $profile = Join-Path $env:TEMP ("edge-shot-" + [guid]::NewGuid().ToString('N'))
    & $edge --headless --disable-gpu --hide-scrollbars --user-data-dir=$profile --screenshot=$out --window-size=1400,900 --virtual-time-budget=9000 $url 2>$null | Out-Null
    Start-Sleep -Seconds 2
    if (Test-Path $out) {
        "OK   $($names[$i])  $([math]::Round((Get-Item $out).Length/1KB,1)) KB"
    } else {
        "FAIL $($names[$i])"
    }
}
