$ErrorActionPreference = 'Stop'

$researchRepo = 'E:\Data\ObsidianVault\04_Tools\senior-kikaku-research'
$researchPython = 'E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe'
$researchLogDir = 'E:\Data\ObsidianVault\04_Tools\logs'
$researchLog = Join-Path $researchLogDir 'senior_priority_archive.log'
$researchUtf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $researchUtf8
$OutputEncoding = $researchUtf8
$env:PYTHONUTF8 = '1'

New-Item -ItemType Directory -Path $researchLogDir -Force | Out-Null
$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -LiteralPath $researchLog -Encoding UTF8 -Value "=== $startedAt priority archive start ==="

Push-Location $researchRepo
try {
    $researchOutput = & $researchPython '.\manus_research\archive_priority_channels.py' 2>&1 |
        Out-String
    $researchExitCode = $LASTEXITCODE
    $researchOutput | Out-File -LiteralPath $researchLog -Append -Encoding utf8
    Write-Output $researchOutput
}
finally {
    Pop-Location
}

$finishedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -LiteralPath $researchLog -Encoding UTF8 -Value "=== $finishedAt priority archive done exit=$researchExitCode ==="
exit $researchExitCode
