# BB-8 R1 Barge-in 与音频来源门控安全设计

本设计只描述本轮已实现的可测试门控，不宣称声纹已形成工具授权系统。

## 1. 四类输入的边界

| 输入 | 播放态处理 | 是否进入正常 ASR/Brain/工具 |
| --- | --- | --- |
| 机器人 TTS 自身回授（普通麦克风帧） | 只进入 `barge_audio_queue`，供 barge-in 探测；队列上限 64 帧，防止无界增长 | 否 |
| 用户真实插话（无显式唤醒） | 仅当声纹服务健康且 `barge_in_owner_ids` 显式命中、分数达阈值时，才作为 barge-in 事件 | 命中后打开一次可信拾音窗口；命中前不进入 |
| 本地唤醒词/按键（设备发 `listen` 文本 `start`/`detect`） | 显式可信拾音事件，立即 abort 当前 TTS 并清旧队列 | 是，作为新输入处理 |
| 声纹结果 | 只产生 barge-in 插话事件，不设置 `current_speaker` 权限，不进入工具执行门控 | 不作为授权依据 |

关键原则：

1. 不使用硬编码 speaker ID 兜底；`barge_in_owner_ids` 为空即拒绝声纹打断。
2. 只有 `VoiceprintProvider.enabled=True`（健康检查通过）时才允许声纹 barge-in。
3. 声纹命中只打开“下一轮拾音窗口”，不证明“说话人有权执行电机/摄像头/文件/命令/进程工具”。
4. 播放期间所有传输路径（WebSocket、MQTT）统一门控；不因 MQTT AEC 存在而放行自声。
5. 不关闭麦克风、不关闭视觉、不禁用正常语音。

## 2. 状态与并发

- `AudioSourceGate.generation`：每次 `suppress/open/rotate` 自增；帧入队时记录 generation。
- 消费侧 `asr_text_priority_thread` 在派发前校验 generation；`handleAudioMessage()` 和 `handle_voice_stop()` 在识别前后再次校验。
- `reset_audio_states(rotate=True)`（默认）在锁内同时完成 generation 递增、ASR/barge 队列清空、VAD 状态复位、`asr_audio`/`_barge_buf` 清空；`rotate=False` 仅用于 `listen stop` 收尾，通过 `AudioSourceGate.seal()` 在同一锁内清队列但保留本轮 generation，使刚复制出的音频仍属于本轮。
- `AudioSourceGate.rotate/suppress/open` 的状态读取、generation 递增、双队列清理与 `normal_open` 写入全部在 `_rotate_locked()` 的同一临界区内完成，无锁窗口。
- `handle_voice_stop()` 使用显式 `source_generation`；ASR、声纹识别、自动注册与最终 `startToChat()` 前均重新校验 generation；`None` 直接 fail-closed，不是有效授权。
- `handleAbortMessage()` 在进入时记录 playback token + sentence；等待 stop 发送期间若新轮次 TTS 已启动，旧 Abort 不再执行 `clearSpeakStatus()`，因此不会清除新播放态或重新开放音频入口。
- `clear_queues()` 也清空两个音频队列和 `asr_audio`，保证 abort 路径不残留。
- `client_is_speaking` 不再是唯一依据：必须同时满足 `tts_playback_active` 与 `tts_playback_token` 才认定播放态。
- `send_tts_message("stop")` 的提前返回、发送异常、过期 sentence 都由 token + sentence 双条件收口；兜底 watchdog 防止异常导致播放态永久关闭拾音。

## 3. 实现位置

| 能力 | 位置 |
| --- | --- |
| 线程安全来源门控 | `server/core/utils/audio_source_gate.py` |
| 统一音频入口 | `server/core/connection.py` `_route_message` / `_process_mqtt_audio_message` / `enqueue_microphone_audio` |
| 播放态进入/退出 | `server/core/connection.py` `begin_tts_playback` / `clearSpeakStatus` |
| barge-in 专用处理 | `server/core/handle/receiveAudioHandle.py` `handleBargeAudioMessage` / `_barge_in_is_owner` |
| 可信拾音事件 | `server/core/handle/textHandler/listenMessageHandler.py` `start`/`detect` 分支 |
| ASR 消费与 generation 校验 | `server/core/providers/asr/base.py` `asr_text_priority_thread` / `_dispatch_audio_item` / `handle_voice_stop` |
| TTS 状态收尾 | `server/core/handle/sendAudioHandle.py` `send_tts_message` / `send_stt_message` |

## 4. 本轮明确不做

- 不在 WebSocket 路径实现伪 AEC；真正的设备侧/协议级 AEC 另立阶段。
- 不把声纹结果接入 `UnifiedToolManager.execute_tool()` 作为授权。
- 不删除远端 Git 历史，不轮换外部 Key，不部署到生产实例。
