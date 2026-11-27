param([string]$RunTag = 'run1', [switch]$NoStart, [switch]$Fresh)

$ErrorActionPreference = 'Stop'
$repo = 'G:\bb8-real-env-test'
$iso  = Join-Path $repo '_isolated'
$evidence = Join-Path $repo ('evidence\real-env-continuous\' + $RunTag)
New-Item -ItemType Directory -Force -Path $iso, $evidence | Out-Null

# Stop only the isolated listener (production bridge stays untouched on 8900).
$conns = Get-NetTCPConnection -LocalPort 8910 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($p -and $p.Path -like '*python*') { Stop-Process -Id $p.Id -Force }
    Start-Sleep -Milliseconds 300
}

if ($Fresh) {
    # WAL files must also be removed: SQLite otherwise recovers the previous run's tasks.
    foreach ($f in @('tasks.db', 'tasks.db-wal', 'tasks.db-shm', 'session_threads.json',
                     'power_reminder_state.json')) {
        Remove-Item -LiteralPath (Join-Path $iso $f) -Force -ErrorAction SilentlyContinue
    }
}

# Sync source from the branch working tree into the isolated runtime.
Get-ChildItem -LiteralPath (Join-Path $repo 'bridge') -Filter '*.py' -File | Copy-Item -Destination $iso -Force

# Real credentials are read from the deployed files at launch time and NEVER committed.
$prodBridge = 'G:\openmuse\xiaozhi-server\bridge\bridge.py'
$prodStart  = 'G:\openmuse\xiaozhi-server\bridge\start_bridge.ps1'
$agnes = [Environment]::GetEnvironmentVariable('AGNES_API_KEY', 'User')
if (-not $agnes) {
    $cfg = Get-Content -LiteralPath 'G:\openmuse\config\config.toml' -Raw
    $m = [regex]::Match($cfg, 'api_key\s*=\s*"([^"]+)"')
    if ($m.Success) { $agnes = $m.Groups[1].Value }
}
if (-not $agnes) { throw 'No real AGNES key found in environment/config' }
$vision = ''
$txt = [System.IO.File]::ReadAllText($prodBridge)
$m = [regex]::Match($txt, 'SF_VISION_KEY\s*=\s*os\.environ\.get\("SF_VISION_KEY",\s*"([^"]+)"\)')
if ($m.Success) { $vision = $m.Groups[1].Value }
if (-not $vision) { $vision = [Environment]::GetEnvironmentVariable('SF_VISION_KEY', 'User') }
if (-not $vision) { throw 'No real SiliconFlow vision key found' }

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:OPENMUSE_BRIDGE_TARGET = 'http://127.0.0.1:8890'
$env:OPENMUSE_BRIDGE_HOST = '127.0.0.1'
$env:OPENMUSE_BRIDGE_PORT = '8910'
$env:OPENMUSE_BRIDGE_POLL = '0.2'
$env:OPENMUSE_BRIDGE_TOKEN_FILE = 'G:\openmuse\data\server_token'
$env:BRIDGE_LLM_KEY = $agnes
$env:OPENMUSE_BRIDGE_LLM_BASE = 'https://apihub.agnes-ai.com/v1'
$env:OPENMUSE_BRIDGE_LLM_MODEL = 'agnes-3.0-flash'
$env:OPENMUSE_BRIDGE_LLM_TIMEOUT = '60'
$env:SF_VISION_KEY = $vision
$env:SF_VISION_MODEL = 'Qwen/Qwen3-VL-8B-Instruct'
$env:OPENMUSE_TASK_DB = Join-Path $iso 'tasks.db'
$env:OPENMUSE_TASK_MAX_WORKERS = '1'
$env:OPENMUSE_TASK_HERMES_CMD = 'G:\Hermes\home\bin\hermes.cmd'
$env:OPENMUSE_TASK_HERMES_MODEL = 'agnes/agnes-3.0-flash'
$env:OPENMUSE_TASK_RESULT_RECEIVER = 'http://127.0.0.1:8910/tasks/result/receiver'
$env:OPENMUSE_TASK_OUTBOX_BACKOFF_BASE = '1'
# Round4: 240s is a test-only timeout for real Hermes skills (direct probe ~22s;
# in-task Chinese prompts plus retries exceeded the old 45s). Production untouched.
$env:OPENMUSE_TASK_TIMEOUT = '240'
$env:BB8_TASK_WAIT_TIMEOUT = '300'
$env:BB8_INJECT_OPENMUSE_MEMORY = '1'
$env:OPENMUSE_BRIDGE_CHAT_TIMEOUT = '35'
$env:POWER_REMINDER_TURNS = '10000'
$env:BB8_DUMP_DIR = $evidence
$env:BB8_TASK_RESTRICTED = '1'

if ($NoStart) { Write-Output 'stopped-only'; return }

$py = 'G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe'
$out = Join-Path $evidence 'bridge.out.log'
$err = Join-Path $evidence 'bridge.err.log'
$p = Start-Process -FilePath $py -ArgumentList 'bridge.py' -WorkingDirectory $iso `
      -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
Write-Output ("isolated_pid=" + $p.Id + " port=8910 evidence=" + $evidence)
