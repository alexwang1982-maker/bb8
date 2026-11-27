$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# 机器人直接用主 OpenMuse（8890）：共享记忆、全部工具、全部技能
$env:OPENMUSE_BRIDGE_TARGET = "http://127.0.0.1:8890"
$env:OPENMUSE_BRIDGE_TOKEN_FILE = "G:\openmuse\data\server_token"
# 轮询间隔从默认 0.8s 降到 0.2s：少等最多 0.6 秒
$env:OPENMUSE_BRIDGE_POLL = "0.2"
# ===== LLM 选择开关：改这一行就能切 agnes / deepseek =====
$env:BRIDGE_LLM = "deepseek"

if ($env:BRIDGE_LLM -eq "deepseek") {
    $env:BRIDGE_LLM_KEY = ""
    $env:OPENMUSE_BRIDGE_LLM_BASE = "https://api.deepseek.com/v1"
    $env:OPENMUSE_BRIDGE_LLM_MODEL = "deepseek-chat"
} else {
    $env:BRIDGE_LLM_KEY = [Environment]::GetEnvironmentVariable("AGNES_API_KEY", "User")
    $env:OPENMUSE_BRIDGE_LLM_BASE = "https://apihub.agnes-ai.com/v1"
    $env:OPENMUSE_BRIDGE_LLM_MODEL = "agnes-3.0-flash"
}
$env:OPENMUSE_BRIDGE_LLM_TIMEOUT = "60"
# C 方案电量估算提醒阈值（轮）；默认 25。测试时可临时改小。
$env:POWER_REMINDER_TURNS = "25"
$py = "G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe"
$log = "G:\openmuse\xiaozhi-server\bridge\bridge.log"
$err = "G:\openmuse\xiaozhi-server\bridge\bridge.err.log"
Start-Process -FilePath $py -ArgumentList "bridge.py" -WorkingDirectory "G:\openmuse\xiaozhi-server\bridge" -RedirectStandardOutput $log -RedirectStandardError $err -WindowStyle Hidden
