$ErrorActionPreference = "Stop"

$PortalDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = "E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe"
$LogDir = Join-Path $PortalDir "logs"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir "deploy_from_bq_$Timestamp.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Start-Transcript -Path $LogPath -Force | Out-Null
try {
  Write-Host "started_at=$(Get-Date -Format o)"
  Write-Host "python=$PythonExe"
  Write-Host "portal=$PortalDir"

  if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python not found: $PythonExe"
  }

  Push-Location -LiteralPath $PortalDir
  try {
    & $PythonExe "deploy_pages.py" "--from-bq"
    if ($LASTEXITCODE -ne 0) {
      throw "deploy_pages.py failed with exit code $LASTEXITCODE"
    }
  } finally {
    Pop-Location
  }

  Write-Host "finished_at=$(Get-Date -Format o)"
} finally {
  Stop-Transcript | Out-Null
}
