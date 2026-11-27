$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:OPENMUSE_BRIDGE_HOST = '127.0.0.1'
$env:OPENMUSE_BRIDGE_PORT = '8901'
$env:OPENMUSE_BRIDGE_TARGET = 'http://127.0.0.1:8890'
$env:OPENMUSE_BRIDGE_TOKEN_FILE = 'G:\openmuse\data\server_token'
$env:OPENMUSE_BRIDGE_POLL = '0.2'
$env:OPENMUSE_BRIDGE_LLM_BASE = 'https://apihub.agnes-ai.com/v1'
$env:OPENMUSE_BRIDGE_LLM_MODEL = 'agnes-3.0-flash'
$env:OPENMUSE_BRIDGE_LLM_TIMEOUT = '60'
$env:BRIDGE_LLM_KEY = [Environment]::GetEnvironmentVariable('AGNES_API_KEY', 'User')
$py = 'G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe'
$wd = 'G:\openmuse\xiaozhi-server\bridge'
$out = 'D:\Temp\opencode\bb8-real-deploy-20261127\evidence\bridge-agnes.out.log'
$err = 'D:\Temp\opencode\bb8-real-deploy-20261127\evidence\bridge-agnes.err.log'
"AGNES_KEY_PRESENT=$([bool]$env:BRIDGE_LLM_KEY)" | Set-Content -LiteralPath 'D:\Temp\opencode\bb8-real-deploy-20261127\evidence\bridge-agnes-env.log' -Encoding UTF8
Start-Process -FilePath $py -ArgumentList 'bridge.py' -WorkingDirectory $wd -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
