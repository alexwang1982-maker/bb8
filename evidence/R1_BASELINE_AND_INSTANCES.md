# BB-8 R1 源码基线与生产实例只读盘点

本文件只记录只读核查结果。未修改生产源码、未部署、未重启、未刷机、未触发硬件。

## 1. 受控工作副本

| 项 | 值 |
| --- | --- |
| 工作副本 | `D:\Temp\opencode\bb8-voice-p0` |
| 分支 | `fix/p0-tts-audio-gate` |
| 基线 commit | `8197bbb58a7805c59a0909fd9d61ea6e7503b0cb` |
| 基线说明 | `baseline: production source snapshot for P0 voice safety R1` |
| 生产服务端源码根 | `G:\openmuse\xiaozhi-server\src\xiaozhi-esp32-server-main\main\xiaozhi-server` |
| 生产桥接层源码根 | `G:\openmuse\xiaozhi-server\bridge` |
| 基线文件数 | 284（服务端 + 桥接层，排除日志/数据库/缓存/备份/启动脚本） |
| 版本关系 | 工作副本是上述两个生产源码根目录的逐文件复制；基线 commit 是复制瞬时快照，无上游、无 `main` 关系。 |
| `G:\bb8-real-env-test` | 有未提交隐私报告改动，未作为本次工作区。 |

基线 SHA256 清单：

- `PRODUCTION_SERVER_SHA256.csv`
- `PRODUCTION_BRIDGE_SHA256.csv`
- `WORKCOPY_SHA256.csv`

关键文件基线 SHA256（修改前，生产与工作副本一致）：

| 文件 | SHA256 |
| --- | --- |
| `server/core/connection.py` | `702c6b0ff444286c1e22a326c0890c2a9daaf024367e1f65e58ba4ed4b2891fb` |
| `server/core/handle/receiveAudioHandle.py` | `317a6ebb04911f36bb12efc45c0a2b44142aeecc0cc39132909b56edbf8a1308` |
| `server/core/handle/sendAudioHandle.py` | `7803519a87e26b046c6246004b0434eae22ae2d445fcc6b56ab29196c14a7080` |
| `server/core/handle/abortHandle.py` | `aa4cf35760918209c2d8da33801747b94302b114ba8ca606ce75d03ba50019a4` |
| `server/core/handle/textHandler/listenMessageHandler.py` | `4ecfaf79c8a1552b2fd0b40060a97ecec391a78f13a7b610e6fc5e88852af678` |
| `server/core/providers/asr/base.py` | `9abc5ad1b2c9b0ef57214e963e7ce7966981084da4b4abaee3c2bd4247c9eb89` |
| `bridge/bridge.py` | `942b7ebf1bee39ac0a00848de53ef90fe3110c511a200283f54e19e9bc933e60` |

未纳入工作副本：`.log`、`.db`、`.bak`、`__pycache__`、`*.ps1` 启动脚本。启动脚本只在原位置只读检查，密钥相关结论见 `R1_KEY_INJECTION_AUDIT.md`。

## 2. 生产实例只读盘点

未重启任何实例。下表为 `Win32_Process` + `Get-NetTCPConnection` 只读结果；Windows WMI 不直接暴露进程 CWD，故未臆测。

| PID | PPID | 可执行文件 | 命令 | 监听端口 |
| ---: | ---: | --- | --- | --- |
| 2660 | 14024 | `G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe` | `app.py` | 无（父进程） |
| 9112 | 2660 | `G:\Hermes\uv-python\cpython-3.11-windows-x86_64-none\python.exe` | `app.py` | `0.0.0.0:8000`, `0.0.0.0:8003`, `127.0.0.1:18083` |
| 12140 | 15888 | `G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe` | `bridge.py` | 无（父进程） |
| 12168 | 12140 | `G:\Hermes\uv-python\cpython-3.11-windows-x86_64-none\python.exe` | `bridge.py` | `127.0.0.1:8900` |
| 16484 | 17240 | `G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe` | `bridge.py` | 无（父进程） |
| 1120 | 16484 | `G:\Hermes\uv-python\cpython-3.11-windows-x86_64-none\python.exe` | `bridge.py` | `127.0.0.1:8910` |
| 1684 | 16528 | `G:\Hermes\home\tools\python-3.14.7+20260901-win32-x64\python.exe` | `-m hermes_cli.main gateway run` | `127.0.0.1:52791` |
| 18744 | 15856 | `G:\Hermes\uv-python\cpython-3.11-windows-x86_64-none\python.exe` | `or_ipv4_connect_proxy.py` | `127.0.0.1:8787` |
| 13316 | 7904 | `G:\Hermes\uv-python\cpython-3.11.16-windows-x86_64-none\python.exe` | `-m openmuse.cli daemon --interval 900` | 无 |
| 15416 | 18964 | `G:\Hermes\uv-python\cpython-3.11.16-windows-x86_64-none\python.exe` | `-m openmuse.cli serve --port 8890` | `0.0.0.0:8890` |

关系说明：

- `8000/8003/18083` 属于 `app.py` 实例；`18083` 是本机 BB-8 安全测试入口。
- 两个 `bridge.py` 实例分别监听 `8900` 与 `8910`。生产启动脚本 `start_bridge.ps1:26` 指定 `WorkingDirectory` 为 `G:\openmuse\xiaozhi-server\bridge`；但当前哪个实例由该脚本启动，只读进程信息无法最终确认，部署前必须确认。
- 8010 无监听，未见 `voiceprint_server.py` 进程；声纹服务当前未运行。
- 本次未启动、未停止、未切换上述任何实例。

## 3. 门禁结论

- 源码归属：已澄清（受控副本 + 独立分支）。
- 实例归属：已列出，但两个 bridge 实例的目标端口/启动来源尚需部署前确认，不得盲重启。
- 依赖：测试使用生产 `.venv2`，Opus 原生库不可用，测试中只使用 Fake decoder；真实验收不依赖 Opus 编解码真实性，证据中已明确标注。
- 密钥：见 `R1_KEY_INJECTION_AUDIT.md`，部署门禁 BLOCKED。
