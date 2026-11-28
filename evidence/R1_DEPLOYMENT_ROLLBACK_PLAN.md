# BB-8 R1 部署与回滚计划（待批准，未执行）

本计划只供审批，**本轮未部署、未重启、未刷机、未触发硬件、未修改生产实例**。

## 1. 部署门禁状态

| 门禁项 | 状态 | 说明 |
| --- | --- | --- |
| R1 自动化新增测试 | PASS | 33/33，见 `R1_VOICE_SAFETY_TEST_OUTPUT.txt` |
| 既有安全回归测试 | FAIL + 挂起（基线固有） | 见 `R1_EXISTING_SAFETY_TEST_OUTPUT.txt` 与 `R1_TEST_EVIDENCE.md` |
| 密钥明确 | **BLOCKED** | 三路密钥无生产注入配置，见 `R1_KEY_INJECTION_AUDIT.md` |
| 实例确认 | **BLOCKED** | 两个 bridge 实例（8900/8910）目标端口与启动来源未最终确认 |
| 回滚准备 | READY | 见第 4 节 |
| 真人声学验收 | NOT DONE | 执行者无法物理完成；预录音频 + 真实 WebSocket 已做，不等同真人声学 |

**结论：不得部署。** 本轮只更新受控分支并推送，等待独立复审。

## 2. 变更清单（受控副本内）

| 文件 | 变更性质 |
| --- | --- |
| `server/core/utils/audio_source_gate.py` | 新增：线程安全来源门控 + generation；复审修正 `rotate()` 原子性、`None` fail-closed、新增 `seal()` |
| `server/core/connection.py` | 音频入口统一门控、播放态、清理、watchdog；复审修正 `reset_audio_states(rotate=...)` |
| `server/core/handle/receiveAudioHandle.py` | barge-in 专用路径、generation 校验、可信入口放行 |
| `server/core/handle/sendAudioHandle.py` | TTS stop 全路径收口 |
| `server/core/handle/abortHandle.py` | abort 先 rotate 再清队列；复审修正 token+sentence 轮次隔离 |
| `server/core/handle/textHandler/listenMessageHandler.py` | listen start/detect 可信拾音；复审修正 stop 保存源 generation |
| `server/core/providers/asr/base.py` | 双队列消费、旧 generation 丢弃；复审修正 `source_generation` 哨兵与显式 `None` 拒绝 |
| `server/core/providers/asr/{doubao,aliyun,aliyunbl,xunfei}_stream.py` | STREAM 会话 generation/WS/task_id 作为任务独立入参；取消实例共享 generation；cleanup 只关闭本会话 WS，close 后按归属确认再清共享状态 |
| `server/core/providers/asr/{doubao,aliyunbl,xunfei}_stream.py` | 本轮新增：connect 前预留会话归属；cleanup 仅凭 `asr_ws is None` 不再清空新会话状态；百炼 task_id 在建立前捕获为局部变量（aliyun 无同类时序，未改） |
| `server/tests/test_bb8_voice_safety_r1.py` | 新增：R1 回归测试（33 用例，含 4 个 STREAM generation 隔离 + 4 个会话资源隔离 + 3 个 connect 窗口隔离用例） |
| `evidence/*` | 新增/更新：证据与方案文档 |

`bridge/bridge.py` 已恢复基线，**不在 R1 交付范围内**；视觉另立功能。

## 3. 部署前置条件（必须全部满足）

1. 独立审查通过本分支最新 commit。
2. 确认目标实例：明确哪个 bridge 实例（PID / 端口 / 启动脚本）承接生产流量；未确认前不重启任何一个。
3. 密钥问题单独解决，或明确「保持源码默认值不变」的临时例外并获得书面批准；本轮代码不含密钥变更。
4. 备份目标实例的当前源码目录与配置（记录 SHA256），确认可逐文件还原。
5. 准备真人声学测试窗口，或接受「预录音频链路」作为有限验收并明确标注局限。

## 4. 回滚方案

### 4.1 代码回滚（逐文件）

基线 SHA256 清单 `PRODUCTION_SERVER_SHA256.csv` / `PRODUCTION_BRIDGE_SHA256.csv` 记录部署前状态。步骤：

1. 在获批维护窗口内停止目标实例。
2. 用备份文件逐文件覆盖 7 个被修改的源码文件；删除新增的 `audio_source_gate.py` 与 R1 测试文件。
3. 用 `Get-FileHash -Algorithm SHA256` 校验每个文件与基线 CSV 一致。
4. 启动实例并冒烟：WebSocket 连接、唤醒词、正常语音对话。视觉链路不在本分支变更范围内，无需因本分支回滚。

### 4.2 配置/密钥回滚

本轮未修改配置或密钥，无需回滚。若后续阶段引入密钥注入，回滚必须按「先恢复旧值 → 再停新注入 → 最后验证」顺序。

### 4.3 行为回滚开关（可选，仅应急）

- `server/core/connection.py`：`is_tts_playback_active()` 直接 `return False` → 恢复旧行为（同时恢复旧风险）。
- 恢复后必须重跑 R1 测试；门控相关用例预期 FAIL，证明回滚生效。

## 5. 部署后验收清单

- [ ] 播放 TTS 期间，预录音频经真实 WebSocket 进入 `barge_audio_queue`，`asr_audio_queue` 保持空。
- [ ] 本地唤醒词/按头 `listen detect/start` 在播放中能打断并打开拾音窗口（需真人设备验证）。
- [ ] `listen stop` 的 ASR 识别在新一轮 reset/abort 后不进入 Brain/声纹注册（需真人或受控并发测试）。
- [ ] 旧 Abort 的 stop 消息不清除新轮 TTS 播放态、不重开音频入口。
- [ ] 声纹 barge-in 在 allowlist 空或 provider 未 enabled 时拒绝。
- [ ] 无新的误触发：播放 TTS 不产生 STT/Brain/tool 调用。
- [ ] 日志出现合理的 `User audio window authorized` 计数，无异常报错。

## 6. 停止条件

出现以下任一情况立即停止部署并回滚：测试出现误触发、实例归属不明、密钥缺失、无法安全区分音频来源、硬件安全策略被触及。
