$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe'
$worker = Join-Path $repoRoot 'manus_research\manus_autopilot.py'
$logDir = Join-Path $repoRoot 'logs'
$stdoutLog = Join-Path $logDir 'manus_autopilot_stdout.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

& $python $worker `
    --profile manus-1.6 `
    --poll-sec 15 `
    --timeout-sec 1200 `
    --idle-sec 300 `
    *>> $stdoutLog

exit $LASTEXITCODE
