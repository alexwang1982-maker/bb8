# BB-8 R1 复审修正交付报告

## 1. 交付范围

本次在既有独立分支 `fix/p0-tts-audio-gate` 上直接追加修正提交，**不修改 `main`、不部署、不重启生产、不刷机、不触发硬件动作、不 force push、不删除远端历史**。

R1 只交付语音安全功能。本轮视觉回灌/3 秒去重改动与对应视觉测试已从 R1 移除，`bridge/bridge.py` 恢复到基线字节（`942b7ebf1bee39ac0a00848de53ef90fe3110c511a200283f54e19e9bc933e60`）。视觉另立功能分支。

## 2. 三项复审并发问题的修复

### 2.1 `listen stop` generation 生命周期

- `handle_voice_stop(..., source_generation=None)` 新增显式源 generation 参数；未显式传入时回退连接当前值，`None` 由 `is_current(None) == False` fail-closed。
- `listenMessageHandler` 的 `stop` 分支在 `reset_audio_states()` **之前**保存 `current_audio_generation`，以 `rotate=False` 收尾：通过 `AudioSourceGate.seal()` 在同锁内清队列但保留本轮 generation，避免刚复制出的音频被自己的收尾误判为旧轮。
- `reset_audio_states(rotate=True)` 仍为真正的新一轮 reset/abort：rotate generation、清双队列、清空 `current_audio_generation`。
- ASR 开始后、声纹自动注册前、最终 `startToChat()` 前均重新校验 generation；新一轮 reset/abort 后旧 ASR 结果、声纹识别结果、自动注册与 Brain 派发全部丢弃。
- 自动 VAD 停止路径（`asr/base.py receive_audio`）同样显式传入 reset 前的 generation。

### 2.2 `handleAbortMessage()` 轮次隔离

- 进入时捕获 `tts_playback_token` 与 `sentence_id`。
- `await websocket.send(tts stop)` 返回后，只有 token 与 sentence 均仍是被打断轮次时才调用 `clearSpeakStatus()`。
- 若等待期间新一轮 TTS 已启动（`begin_tts_playback` 生成新 token），旧 Abort 不再清除新播放态、不再调用 `audio_gate.open()` 重开新一轮音频入口。
- 无新轮次时仍正常收口，避免播放态永久关闭拾音。

### 2.3 `AudioSourceGate.rotate()` 原子性

- 原 `_rotate()` 在锁外读取 `normal_open` 再进入 `_rotate()`，存在锁窗口；现改为 `_rotate_locked()` 必须在持有 `self._lock` 时调用。
- `rotate()` / `suppress()` / `open()` 均在同一个 `with self._lock` 内完成：**读取状态 → generation 递增 → 双队列清理 → normal_open 保留/写入**。
- `seal()` 也在同锁内完成队列清理与 generation 读取。

## 3. 视觉范围移除

| 项 | 处理 |
| --- | --- |
| `bridge/bridge.py` | 恢复基线 blob `c45bccf...`，文件 SHA256 `942b7ebf...`，与生产基线一致 |
| `_vision_recent` / `VISION_DEDUP_SECS` / `is_vision_continuation()` / `vision_request_allowed()` | 全部移除 |
| `TestVisionRouting`（2 个视觉测试） | 从 R1 测试文件删除 |
| 视觉相关交付文档表述 | 更新为「视觉另立功能，不在 R1 交付」 |

保留 `vision_look`、`self_camera_send`、`self_camera_latest` 三种独立能力不变（本分支不触碰它们）。

## 4. 测试证据

### 4.1 R1 新增测试：20/20 PASS

`R1_VOICE_SAFETY_TEST_OUTPUT.txt`：`Ran 20 tests in 0.708s` / `OK`。较上一版（14 项，含 2 项视觉）净增 8 项针对性回归：

| 用例 | 断言 |
| --- | --- |
| `test_listen_stop_passes_source_generation_before_reset` | stop 收尾前保存源 generation，收尾不 rotate |
| `test_new_round_during_asr_drops_brain_dispatch` | 识别中新一轮 rotate → Brain 不被调用 |
| `test_old_voiceprint_enrollment_dropped_after_new_round` | 新轮次后旧声纹自动注册不执行 |
| `test_none_generation_is_not_authorization` | `None` 不是授权，STT 不启动 |
| `test_stale_abort_does_not_clear_new_playback` | 旧 Abort 不清新 token 播放态，入口保持关闭 |
| `test_abort_without_new_round_still_clears` | 正常 Abort 仍收口 |
| `test_rotate_holds_lock_for_whole_state_change` | `rotate()` 全程持锁 |
| `test_concurrent_gate_changes_keep_generation_consistent` | 6 线程 × 300 次并发变更 generation 一致 |

（其中 generation 生命周期 4 项、Abort 2 项、锁原子性 2 项，共 8 项；原 14 项扣除 2 项视觉测试后为 12 项，合计 20 项。）

### 4.2 既有安全回归：基线固有 FAIL + 挂起

`R1_EXISTING_SAFETY_TEST_OUTPUT.txt` 如实记录：`test_ambiguous_head_result_stops_without_second_motor_or_post_status ... FAIL`，随后 `test_concurrent_trigger_is_rejected_as_busy` 挂起，60 秒后终止进程树，未产出 `Ran/OK/FAILED` 汇总。同一表现此前已在未修改基线 8197bbb 复现；本轮未修复、未宣称通过。

### 4.3 真实验收边界

真实本机 WebSocket + 预录帧的入口门控已验证（`normal_enqueued=0, barge_enqueued=1`）；真人声学插话未执行，需部署审批后由现场人员完成。

## 5. 证据文件清单

| 文件 | 内容 |
| --- | --- |
| `R1_FULL_DIFF.patch` | 从基线 `8197bbb` 到最终工作副本的完整 diff（不含补丁文件自身，避免自引用） |
| `R1_VOICE_SAFETY_TEST_OUTPUT.txt` | R1 20 项测试原始输出 |
| `R1_EXISTING_SAFETY_TEST_OUTPUT.txt` | 既有安全套件 FAIL/挂起原始记录 |
| `R1_TEST_EVIDENCE.md` | 测试覆盖与验收边界 |
| `WORKCOPY_AFTER_SHA256.csv` | 最终工作副本关键文件 size + SHA256 |
| `R1_BARGE_IN_SAFETY_DESIGN.md` | 门控设计（含本轮三项并发修正） |
| `R1_BASELINE_AND_INSTANCES.md` | 基线与运行态盘点（只读） |
| `R1_VOICEPRINT_AUTHORIZATION_AUDIT.md` | 声纹/授权链审计 |
| `R1_KEY_INJECTION_AUDIT.md` | 密钥注入只读审计 |
| `R1_REAL_AEC_DESIGN.md` | 真实 AEC 设备侧/协议级方案（未实现） |
| `R1_DEPLOYMENT_ROLLBACK_PLAN.md` | 部署门禁与回滚（待批准） |
| `R1_DELIVERY_REPORT.md` | 本交付报告 |

最终 commit SHA 与远端校验以推送回执、`git ls-remote` 输出为准；本报告与代码在同一提交内，不声称预知 SHA。

## 6. 未执行事项

- 未部署、未重启、未刷机、未触发电机/解锁/硬件安全策略。
- 未修改生产实例、配置、数据库、启动脚本；未轮换或输出任何 API Key。
- 未修改 `main`，未 force push，未删除远端历史。
- 未在 WebSocket 路径实现伪 AEC；真实 AEC 仅保留技术方案。
- 未将声纹接入工具授权链；声纹 barge-in 仍受 allowlist + 阈值 + provider 健康三重门控。

## 7. 停止条件与后续

部署仍 BLOCKED：密钥注入未明确、bridge 实例归属未最终确认、真人声学验收未执行。等待独立复审通过后，由获批人员在维护窗口内按 `R1_DEPLOYMENT_ROLLBACK_PLAN.md` 执行。
