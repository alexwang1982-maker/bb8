# BB-8 R1 测试与验收证据

## 1. 新增 R1 安全测试：PASS

命令（受控副本 `server/` 目录，生产 `.venv2`，`-B` 禁止写 pyc）：

```
G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe -B -m unittest tests.test_bb8_voice_safety_r1 -v
```

原始输出：`R1_VOICE_SAFETY_TEST_OUTPUT.txt`

```
Ran 14 tests in 0.501s
OK
```

14 个用例覆盖：

| 用例 | 覆盖点 |
| --- | --- |
| `test_playback_frames_go_only_to_barge_queue` | 播放态帧只进 barge 队列，不进 ASR |
| `test_normal_audio_flows_when_not_playing` | 非播放态正常入 ASR |
| `test_reset_drains_queues_and_invalidates_old_frames` | reset 清双队列/VAD/asr_audio 并使旧 generation 失效 |
| `test_receive_audio_handle_is_not_called_for_barge_frames` | barge 帧绝不派发到正常 ASR 处理函数 |
| `test_stale_frames_are_not_dispatched` | 过期 generation 帧不派发 |
| `test_websocket_entry_gates_during_playback` | WebSocket 入口门控 |
| `test_mqtt_aec_false_does_not_apply_aec` | `aec:false` 时不调用 `_apply_aec` |
| `test_mqtt_aec_true_with_valid_timestamp_applies_aec` | `aec:true` + 合法 timestamp 才执行 AEC |
| `test_mqtt_audio_is_also_barge_gated_during_playback` | MQTT 入口同样受门控 |
| `test_stop_clears_speaking_even_when_send_fails` | stop 异常路径仍清播放态 |
| `test_stale_sentence_stop_does_not_clear_current_playback` | 过期 sentence 的 stop 不误清新播放态 |
| `test_three_capabilities_stay_separate` | 视觉/摄像头管理/摄像头最新三种能力互不串线 |
| `test_vision_continuation_and_dedup` | 视觉回灌识别 + 3 秒去重 |
| `test_playback_frames_over_real_websocket_never_enter_asr_queue` | 真实 WebSocket 连接下播放帧不入 ASR 队列 |

其中最后一项启动本机临时 `websockets` 服务（临时端口），经真实 WebSocket 连接发送帧并验证路由结果；**使用 Fake Opus decoder，不等同真人声学测试**。

## 2. 既有安全回归：失败为基线固有，与本轮无关

命令：`python -B -m unittest tests.test_bb8_safe_test -v`

在**受控副本**与**未修改基线 worktree（8197bbb）**上分别运行，两者在完全相同位置出现同一失败并同样挂起：

```
test_safe_interface_is_awaited_before_main_services ... ok
test_ambiguous_head_result_stops_without_second_motor_or_post_status ... FAIL
test_closed_connection_removes_connection_without_mcp_call ... ok
...（随后进程挂起，未输出 Ran/OK/FAILED）
```

证据：

- `R1_EXISTING_SAFETY_TEST_OUTPUT.txt`（受控副本）
- `BASELINE_SAFETY_TEST_OUTPUT.txt`（基线 worktree，已清理）

判定：该用例属于 `core/bb8_safe_test.py` 头部电机歧义处理逻辑，本轮**未修改**该文件；基线行为一致，故不构成 R1 回归。挂起原因未在本轮定位，列为既有问题。由于该套件无法完整跑完，不能据此宣称「既有 33 用例全通过」。

## 3. 真实验收边界

- 已完成：真实服务端 `ConnectionHandler._route_message` + 本机真实 WebSocket 连接 + 预录帧；门控计数 `normal_enqueued=0, barge_enqueued=1`。
- 未完成：真人声学插话（执行者无法物理完成），需部署审批后由现场人员执行。
- 生产运行态复核（只读）：8010 无监听、无 `voiceprint_server.py` 进程、`voiceprint_server_unavailable=24`、成功声纹/打断标记 0、`aec:false` 24 次、`aec:true` 0、MQTT 记录 0、`[AEC]` 0；bridge 日志 `vision_look=45`、`vision context blocked=9`，含「看看」的 4 条均进入 `vision_look`，设备摄像头工具 0。

## 4. 结论

- R1 新增门控逻辑的自动化验证充分且通过。
- 既有安全套件的失败/挂起为基线固有，需单独立项；本轮不试图修它（避免超出任务范围、避免引入无关变更）。
- 真人声学与部署均待批准，按门禁执行。
