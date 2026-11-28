# BB-8 R1 复审修正交付报告

## 1. 交付范围

本次在既有独立分支 `fix/p0-tts-audio-gate` 上直接追加修正提交，**不修改 `main`、不部署、不重启生产、不刷机、不触发硬件动作、不 force push、不删除远端历史**。

R1 只交付语音安全功能。本轮视觉回灌/3 秒去重改动与对应视觉测试已从 R1 移除，`bridge/bridge.py` 恢复到基线字节（`942b7ebf1bee39ac0a00848de53ef90fe3110c511a200283f54e19e9bc933e60`）。视觉另立功能分支。

## 2. 三项复审并发问题的修复

### 2.1 `listen stop` generation 生命周期

- `handle_voice_stop()` 使用哨兵 `SOURCE_GENERATION_UNSET` 区分“未传参数”和“明确传 `None`”：未传才回退连接当前 generation（兼容旧调用）；**明确 `None` 立即返回**，不回退、不借用当前 generation，后续处理全部不启动。
- `listenMessageHandler` 的 `stop` 分支在 `reset_audio_states()` **之前**保存 `current_audio_generation`，以 `rotate=False` 收尾：通过 `AudioSourceGate.seal()` 在同锁内清队列但保留本轮 generation，避免刚复制出的音频被自己的收尾误判为旧轮。
- `reset_audio_states(rotate=True)` 仍为真正的新一轮 reset/abort：rotate generation、清双队列、清空 `current_audio_generation`。
- ASR 开始后、声纹自动注册前、最终 `startToChat()` 前均重新校验 generation；新一轮 reset/abort 后旧 ASR 结果、声纹识别结果、自动注册与 Brain 派发全部丢弃。
- 自动 VAD 停止路径（`asr/base.py receive_audio`）同样显式传入 reset 前的 generation。
- **STREAM 全路径**（`doubao_stream.py`、`aliyun_stream.py`、`aliyunbl_stream.py`、`xunfei_stream.py`）在识别会话建立时捕获当时的 `current_audio_generation`，并作为独立参数传给结果任务 `_forward_results(conn, source_generation)` / `_forward_asr_results(conn, source_generation)`；任务全程只用自己的入参，不再读取连接当前 generation 或 Provider 共享属性。`xunfei_stream.py` 的覆写也透传该参数。新轮次 rotate 后旧会话回调被拒绝，旧识别不能进入新轮次 Brain。

### 2.2 `handleAbortMessage()` 轮次隔离

- 进入时捕获 `tts_playback_token` 与 `sentence_id`。
- `await websocket.send(tts stop)` 返回后，只有 token 与 sentence 均仍是被打断轮次时才调用 `clearSpeakStatus()`。
- 若等待期间新一轮 TTS 已启动（`begin_tts_playback` 生成新 token），旧 Abort 不再清除新播放态、不再调用 `audio_gate.open()` 重开新一轮音频入口。
- 无新轮次时仍正常收口，避免播放态永久关闭拾音。

### 2.3 `AudioSourceGate.rotate()` 原子性

- 原 `_rotate()` 在锁外读取 `normal_open` 再进入 `_rotate()`，存在锁窗口；现改为 `_rotate_locked()` 必须在持有 `self._lock` 时调用。
- `rotate()` / `suppress()` / `open()` 均在同一个 `with self._lock` 内完成：**读取状态 → generation 递增 → 双队列清理 → normal_open 保留/写入**。
- `seal()` 也在同锁内完成队列清理与 generation 读取。

### 2.4 STREAM 结果任务 generation 参数化（复审）

- 四种 STREAM Provider 全部取消 `_stream_source_generation` 实例共享属性：旧任务的 `finally` 不再把新会话的 generation 置空。
- 会话建立时把捕获的 generation 作为协程参数交给结果任务；旧任务即使在新轮次之后才执行，仍持有创建时的旧 generation。
- 四种 Provider 的 `finally` 清理只在 `conn.audio_gate.is_current(source_generation)` 为真时调用 `conn.reset_audio_states()`；旧会话 cleanup 不会 rotate/清空新会话已取得的 generation。
- 新增 4 项并发回归（每种 Provider 一项）：旧任务先创建不执行 → 新轮次启动并取得新 generation → 旧任务执行时用旧 generation 且 Brain 不被调用 → 新任务用新 generation 正常派发 Brain；讯飞覆写透传也单独断言。

### 2.5 STREAM 会话资源隔离（复审）

- 四种 STREAM Provider 的 WebSocket、task_id 与连接状态在会话建立时捕获为会话局部对象；结果任务只调用该 WebSocket 收/发，停止请求也只作用于调用时所属的会话对象。
- `_cleanup(session_ws, session_task_id)` 只关闭传入的旧 WebSocket；`await close()` 期间若新会话已占用 Provider，则再次确认归属，只有仍持有同一 WS（或尚未有新会话）时才清空 `asr_ws` / `is_processing` / `server_ready` / `forward_task` / `task_id`。旧 cleanup 不再关闭或清空新会话资源。
- `stop_ws_connection()` 同样先捕获旧 WS 再置空共享引用；启动失败路径也关闭会话局部 WS，避免异常清理误伤新会话。
- 新增 4 项同一 Provider 实例异步交错回归：旧任务进入 `cleanup` 并暂停在 WebSocket close → 新轮次 rotate → 在同一实例启动新会话 → 释放旧 close → 断言新 WS 未被关闭、`asr_ws` / `forward_task` / `is_processing` 均未被旧任务破坏，且新任务随后正常完成并派发 Brain。

### 2.6 STREAM connect 返回前的会话归属预留（最终复审）

- 覆盖范围仅 doubao、aliyunbl、xunfei 三个已确认存在该时序的 Provider；aliyun 不修改。
- 三个 Provider 在 `websockets.connect()` 之前（任何 await 之前）设置会话局部 `_session_reservation` 标记；`is_processing`（以及百炼的 `task_id`）同样在 connect 前以本轮局部值写入。connect 返回、共享状态提交后再清除标记。
- `_cleanup()` 在 `await close()` 之后重新确认归属：仅当仍持有同一 WS，或 `asr_ws is None` 且**没有新会话预留**时，才清空共享状态。旧 cleanup 不能仅凭 `self.asr_ws is None` 判定“无人占用”。
- 百炼的 `session_task_id` 在会话建立前捕获为局部变量，run-task 与 finish-task 始终使用该局部值，不再在 await 之后回读 `self.task_id`。
- 启动失败路径先清除本轮预留再清理，避免失败残留标记阻塞后续收尾。
- 新增 3 项确定性异步交错回归：旧 cleanup 暂停在 close → 新会话 connect 暂停且尚未返回（`asr_ws` 仍为 None）→ 释放旧 close → 断言 `is_processing` / 预留标记 / `forward_task` / 百炼 `task_id` 未被清空 → 释放 connect → 新会话完成建立并正常派发 Brain。
- **aliyun 无同类问题**：`_start_recognition()` 把 `self.asr_ws` / `self.task_id` / `is_processing` / `server_ready` / `forward_task` 的全部写入放在 `await websockets.connect()` 返回之后；connect 窗口内 Provider 仍处于干净的空闲态，旧 cleanup 即使清空也只是重复置空，新会话返回后会重新提交全部状态。因此本轮不改 aliyun。

## 3. 视觉范围移除

| 项 | 处理 |
| --- | --- |
| `bridge/bridge.py` | 恢复基线 blob `c45bccf...`，文件 SHA256 `942b7ebf...`，与生产基线一致 |
| `_vision_recent` / `VISION_DEDUP_SECS` / `is_vision_continuation()` / `vision_request_allowed()` | 全部移除 |
| `TestVisionRouting`（2 个视觉测试） | 从 R1 测试文件删除 |
| 视觉相关交付文档表述 | 更新为「视觉另立功能，不在 R1 交付」 |

保留 `vision_look`、`self_camera_send`、`self_camera_latest` 三种独立能力不变（本分支不触碰它们）。

## 4. 测试证据

### 4.1 R1 新增测试：33/33 PASS

`R1_VOICE_SAFETY_TEST_OUTPUT.txt`：`Ran 33 tests in 2.459s` / `OK`。较上一版（30 项）新增 3 项 STREAM connect 窗口交错回归：

| 用例 | 断言 |
| --- | --- |
| `test_listen_stop_passes_source_generation_before_reset` | stop 收尾前保存源 generation，收尾不 rotate |
| `test_new_round_during_asr_drops_brain_dispatch` | 识别中新一轮 rotate → Brain 不被调用 |
| `test_old_voiceprint_enrollment_dropped_after_new_round` | 新轮次后旧声纹自动注册不执行 |
| `test_none_generation_is_not_authorization` | `None` 不是授权，STT 不启动 |
| **`test_explicit_none_rejected_even_when_connection_generation_valid`** | 连接 generation 有效但旧回调显式传 `None`：STT、`stop_ws_connection`、ASR 上报、声纹识别、自动注册、Brain 全部拒绝 |
| **`test_stale_stream_captured_generation_rejected_after_new_round`** | STREAM 捕获的旧 generation 在新轮次后失效；连接当前 generation 有效也不得派发 |
| **`test_doubao_stream_task_keeps_own_generation`** | doubao：旧任务延后执行仍传旧 generation，Brain 不派发；新任务传新 generation 并派发 Brain |
| **`test_aliyun_stream_task_keeps_own_generation`** | aliyun：同上；旧任务 cleanup 不无效化新 generation |
| **`test_aliyunbl_stream_task_keeps_own_generation`** | aliyunbl：同上 |
| **`test_xunfei_stream_task_keeps_own_generation`** | xunfei：同上，并断言覆写把 generation 原样透传基类 |
| **`test_doubao_old_cleanup_does_not_destroy_new_session`** | doubao：旧 cleanup 暂停 close 时新会话启动；释放后新 WS/forward_task/processing 未破坏，新任务完成 |
| **`test_aliyun_old_cleanup_does_not_destroy_new_session`** | aliyun：同上 |
| **`test_aliyunbl_old_cleanup_does_not_destroy_new_session`** | aliyunbl：同上，finish-task 也只作用于旧会话 WS/task_id |
| **`test_xunfei_old_cleanup_does_not_destroy_new_session`** | xunfei：同上，最后一帧只发给旧会话 WS |
| **`test_doubao_connect_window_is_not_cleared_by_old_cleanup`** | doubao：新会话 connect 暂停且 `asr_ws` 为 None 时旧 cleanup 返回，预留状态不被清空；connect 后正常完成 |
| **`test_aliyunbl_connect_window_is_not_cleared_by_old_cleanup`** | aliyunbl：同上，并断言 connect 前捕获的局部 `task_id` 未被清空且 run-task 使用该局部值 |
| **`test_xunfei_connect_window_is_not_cleared_by_old_cleanup`** | xunfei：同上 |
| `test_stale_abort_does_not_clear_new_playback` | 旧 Abort 不清新 token 播放态，入口保持关闭 |
| `test_abort_without_new_round_still_clears` | 正常 Abort 仍收口 |
| `test_rotate_holds_lock_for_whole_state_change` | `rotate()` 全程持锁 |
| `test_concurrent_gate_changes_keep_generation_consistent` | 6 线程 × 300 次并发变更 generation 一致 |

（generation 生命周期 6 项、STREAM 任务 generation 隔离 4 项、STREAM 会话资源隔离 4 项、STREAM connect 窗口隔离 3 项、Abort 2 项、锁原子性 2 项；其余 12 项为门控/入口/TTS/真实 WebSocket，合计 33 项。）

### 4.2 既有安全回归：基线固有 FAIL + 挂起

`R1_EXISTING_SAFETY_TEST_OUTPUT.txt` 如实记录：`test_ambiguous_head_result_stops_without_second_motor_or_post_status ... FAIL`，随后 `test_concurrent_trigger_is_rejected_as_busy` 挂起，60 秒后终止进程树，未产出 `Ran/OK/FAILED` 汇总。同一表现此前已在未修改基线 8197bbb 复现；本轮保留原始失败记录，未重跑、未修复、未宣称通过。

### 4.3 真实验收边界

真实本机 WebSocket + 预录帧的入口门控已验证（`normal_enqueued=0, barge_enqueued=1`）；真人声学插话未执行，需部署审批后由现场人员完成。

## 5. 证据文件清单

| 文件 | 内容 |
| --- | --- |
| `R1_FULL_DIFF.patch` | 从基线 `8197bbb` 到最终工作副本的完整 diff（不含补丁文件自身，避免自引用） |
| `R1_VOICE_SAFETY_TEST_OUTPUT.txt` | R1 33 项测试原始输出 |
| `R1_EXISTING_SAFETY_TEST_OUTPUT.txt` | 既有安全套件 FAIL/挂起原始记录 |
| `R1_TEST_EVIDENCE.md` | 测试覆盖与验收边界 |
| `WORKCOPY_AFTER_SHA256.csv` | 最终工作副本关键文件 size + SHA256 |
| `R1_BARGE_IN_SAFETY_DESIGN.md` | 门控设计（含五轮复审并发修正） |
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
