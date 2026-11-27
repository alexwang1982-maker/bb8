# BB-8 R1 部署与回滚计划（待批准，未执行）

本计划只供审批，**本轮未部署、未重启、未刷机、未触发硬件、未修改生产实例**。

## 1. 部署门禁状态

| 门禁项 | 状态 | 说明 |
| --- | --- | --- |
| R1 自动化新增测试 | PASS | 14/14，见 `R1_VOICE_SAFETY_TEST_OUTPUT.txt` |
| 既有安全回归测试 | 与本轮无关 | `test_ambiguous_head_result_stops_without_second_motor_or_post_status` 在未修改基线上同样 FAIL 并同样挂起；详见 `R1_TEST_EVIDENCE.md` |
| 密钥明确 | **BLOCKED** | 三路密钥无生产注入配置，见 `R1_KEY_INJECTION_AUDIT.md` |
| 实例确认 | **BLOCKED** | 两个 bridge 实例（8900/8910）目标端口与启动来源未最终确认 |
| 回滚准备 | READY | 见第 4 节 |
| 真人声学验收 | NOT DONE | 执行者无法物理完成；预录音频 + 真实 WebSocket 已做，但不等同真人声学 |

**结论：不得部署。** 本轮只提交工作副本 diff 与 commit 等待独立审查。

## 2. 变更清单（受控副本内）

| 文件 | 变更性质 |
| --- | --- |
| `server/core/utils/audio_source_gate.py` | 新增：线程安全来源门控 + generation |
| `server/core/connection.py` | 音频入口统一门控、播放态、清理、watchdog |
| `server/core/handle/receiveAudioHandle.py` | barge-in 专用路径、generation 校验、可信入口放行 |
| `server/core/handle/sendAudioHandle.py` | TTS stop 全路径收口 |
| `server/core/handle/abortHandle.py` | abort 先 rotate 再清队列 |
| `server/core/handle/textHandler/listenMessageHandler.py` | listen start/detect 可信拾音事件 |
| `server/core/providers/asr/base.py` | 双队列消费、旧 generation 丢弃 |
| `bridge/bridge.py` | 视觉回灌识别 + 3 秒去重 |
| `server/tests/test_bb8_voice_safety_r1.py` | 新增：R1 回归测试 |
| `evidence/*` | 新增：证据与方案文档 |

## 3. 部署前置条件（必须全部满足）

1. 独立审查通过本 diff 与 commit。
2. 确认目标实例：明确哪个 bridge 实例（PID / 端口 / 启动脚本）承接生产流量；未确认前不重启任何一个。
3. 密钥问题单独解决或明确「保持源码默认值不变」的临时例外并获得书面批准；本轮代码不含密钥变更，可在默认值不变前提下部署。
4. 备份目标实例的当前源码目录与配置（记录 SHA256），确认可逐文件还原。
5. 准备真人声学测试窗口（需要人 physically 在场插话），或接受「预录音频链路」作为有限验收并明确标注局限。

## 4. 回滚方案

### 4.1 代码回滚（逐文件）

基线 SHA256 清单 `PRODUCTION_SERVER_SHA256.csv` / `PRODUCTION_BRIDGE_SHA256.csv` 记录部署前状态。回滚步骤：

1. 停止目标实例（仅在获批的维护窗口内）。
2. 用备份文件逐文件覆盖被修改的 8 个源码文件；删除新增的 `audio_source_gate.py` 与 R1 测试文件。
3. 用 `Get-FileHash -Algorithm SHA256` 校验每个文件与基线 CSV 一致。
4. 启动实例并冒烟：WebSocket 连接、唤醒词、视觉 `vision_look`、摄像头管理三种能力分别验证。

### 4.2 配置/密钥回滚

本轮未修改配置或密钥，无需回滚。若后续阶段引入密钥注入，回滚必须按「先恢复旧值 → 再停新注入 → 最后验证」顺序，避免能力中断。

### 4.3 行为回滚开关（可选）

在受控副本中可通过以下最小改动临时恢复旧行为（仅作应急，需重新审查）：

- `server/core/connection.py`：`is_tts_playback_active()` 直接 `return False` → 门控不再关闭正常入口（恢复旧行为，也恢复旧风险）。
- `bridge/bridge.py`：`vision_request_allowed()` 直接 `return vision_request(text)` → 关闭去重。
- 恢复后必须重新跑 R1 测试；预期门控相关用例 FAIL（这是预期，证明回滚生效）。

## 5. 部署后验收清单

- [ ] 播放 TTS 期间，预录音频经真实 WebSocket 进入 `barge_audio_queue`，`asr_audio_queue` 保持空（已有自动化证据）。
- [ ] 本地唤醒词/按头 `listen detect/start` 在播放中能打断并打开拾音窗口（需真人设备验证）。
- [ ] 声纹 barge-in 在 `barge_in_owner_ids` 空时拒绝；provider 未 enabled 时拒绝（单元可测）。
- [ ] `vision_look` 回灌轮不重复抓图，3 秒去重生效；`vision_look`/`self_camera_send`/`self_camera_latest` 三种能力互不串线。
- [ ] 无新的误触发：播放 TTS 不产生 STT/Brain/tool 调用。
- [ ] 日志出现合理的 `User audio window authorized` / `vision_look skip` 计数；无异常报错。

## 6. 停止条件

出现以下任一情况立即停止部署并回滚：测试出现误触发、实例归属不明、密钥缺失、无法安全区分音频来源、硬件安全策略被触及。
