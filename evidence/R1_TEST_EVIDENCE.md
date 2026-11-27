# BB-8 R1 测试与验收证据（复审修正后）

## 1. R1 安全测试：20/20 PASS

命令（受控副本 `server/` 目录，生产 `.venv2`，`-B` 禁止写 pyc）：

```
G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe -B -m unittest tests.test_bb8_voice_safety_r1 -v
```

原始输出：`R1_VOICE_SAFETY_TEST_OUTPUT.txt`

```
Ran 20 tests in 0.708s
OK
```

输出中无 ERROR / FAIL 日志。覆盖点：

| 分组 | 用例 | 覆盖点 |
| --- | --- | --- |
| 来源门控 | `test_playback_frames_go_only_to_barge_queue` | 播放态帧只进 barge 队列 |
| | `test_normal_audio_flows_when_not_playing` | 非播放态正常入 ASR |
| | `test_reset_drains_queues_and_invalidates_old_frames` | reset 清双队列/VAD 并使旧 generation 失效 |
| | `test_receive_audio_handle_is_not_called_for_barge_frames` | barge 帧绝不进正常 ASR 处理 |
| | `test_stale_frames_are_not_dispatched` | 过期 generation 帧不派发 |
| 入口/AEC | `test_websocket_entry_gates_during_playback` | WebSocket 入口门控 |
| | `test_mqtt_aec_false_does_not_apply_aec` | `aec:false` 不调用 `_apply_aec` |
| | `test_mqtt_aec_true_with_valid_timestamp_applies_aec` | `aec:true` + timestamp 才执行 AEC |
| | `test_mqtt_audio_is_also_barge_gated_during_playback` | MQTT 入口同样门控 |
| TTS 状态 | `test_stop_clears_speaking_even_when_send_fails` | stop 异常路径收口 |
| | `test_stale_sentence_stop_does_not_clear_current_playback` | 过期 sentence 的 stop 不误清新播放态 |
| **复审新增：`listen stop` generation** | `test_listen_stop_passes_source_generation_before_reset` | stop 收尾前保存源 generation，且收尾不 rotate |
| | `test_new_round_during_asr_drops_brain_dispatch` | 识别等待期间新一轮 reset → 旧结果不进 Brain |
| | `test_old_voiceprint_enrollment_dropped_after_new_round` | 新轮次后旧声纹自动注册不执行 |
| | `test_none_generation_is_not_authorization` | `None` 不是有效授权，`is_current(None)=False` 且 STT 不启动 |
| **复审新增：Abort 轮次隔离** | `test_stale_abort_does_not_clear_new_playback` | 旧 Abort 不清除新 token/sentence 的播放态，音频入口保持关闭 |
| | `test_abort_without_new_round_still_clears` | 无新轮次时 Abort 正常收口 |
| **复审新增：门控原子性** | `test_rotate_holds_lock_for_whole_state_change` | `rotate()` 全程持锁 |
| | `test_concurrent_gate_changes_keep_generation_consistent` | 并发 6 线程 × 300 次状态变更无 generation 丢失 |
| 真实 WS | `test_playback_frames_over_real_websocket_never_enter_asr_queue` | 真实本机 WebSocket 下播放帧不入 ASR 队列 |

最后一项启动本机临时 `websockets` 服务（临时端口），使用 Fake Opus decoder；**不等同真人声学测试**。

## 2. 既有安全回归：基线固有 FAIL + 挂起，未修

命令：`python -B -m unittest tests.test_bb8_safe_test -v`

原始输出：`R1_EXISTING_SAFETY_TEST_OUTPUT.txt`（含命令、运行时长、双流合并、挂起说明）

实测结果：

```
test_safe_interface_is_awaited_before_main_services ... ok
test_ambiguous_head_result_stops_without_second_motor_or_post_status ... FAIL
test_closed_connection_removes_connection_without_mcp_call ... ok
test_concurrent_trigger_is_rejected_as_busy ...     <-- 此处挂起
```

60 秒后按进程树终止，**未产出 `Ran/OK/FAILED` 汇总**。同一失败与挂起此前已在未修改基线 worktree（8197bbb）复现，属既有问题，本轮**未尝试修复**（超出 R1 语音安全范围）。因此不能宣称「既有 33 用例全部通过」。

## 3. 视觉范围

R1 复审修正已移除 `bridge/bridge.py` 的视觉回灌/3 秒去重改动及对应 2 个视觉测试。`bridge/bridge.py` 已恢复基线 SHA256 `942b7ebf1bee39ac0a00848de53ef90fe3110c511a200283f54e19e9bc933e60`，与 `PRODUCTION_BRIDGE_SHA256.csv` 一致。视觉能力另立功能，不在本分支交付。

## 4. 真实验收边界

- 已完成：真实 `ConnectionHandler._route_message` + 本机真实 WebSocket + 预录帧；门控计数 `normal_enqueued=0, barge_enqueued=1`。
- 未完成：真人声学插话（执行者无法物理完成），需部署审批后由现场人员执行。
- 生产运行态只读复核（来自上一轮盘点，本轮未再触碰实例）：8010 无监听、`aec:false` 24 次 / `aec:true` 0、`[AEC]` 0、`voiceprint_server_unavailable=24`。

## 5. 结论

三项复审并发问题均有针对性回归测试且通过；既有安全套件的失败/挂起如实记录为基线固有；R1 不再包含视觉功能。部署仍待批准，门禁状态见 `R1_DEPLOYMENT_ROLLBACK_PLAN.md`。
