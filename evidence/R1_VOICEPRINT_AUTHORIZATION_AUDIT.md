# BB-8 R1 声纹与授权链审计结论

复核对象：当前生产源码与受控副本；此前两路只读审计结论已回传，本轮未重新审计已确认内容，只复核与本次修复直接相关的位置。所有 speaker ID、姓名、URL 查询参数值均已脱敏。

## 1. 总判定

- 声纹提取、注册、识别代码真实存在，并在连接初始化和 ASR 语音停止时执行路径中被调用。
- 但当前运行态声纹服务未运行（8010 无监听，无对应进程），`VoiceprintProvider` 健康检查失败，`enabled=False`；不能宣称声纹认证有效。
- 识别结果只进入 ASR JSON、`conn.current_speaker`、LLM system prompt 的 `<speakers_info>`，以及 barge-in 打断判断；没有进入工具执行授权链。
- `UnifiedToolManager.execute_tool()`、`unified_tool_handler.py`、bridge 快通道、MCP/IoT/插件/文件转发链均不读取声纹身份或 owner 权限。
- 因此：声纹是“识别/上下文/打断过滤”能力，不是授权系统。

## 2. 关键链路

```text
音频 → ASR 语音停止
  → asr/base.py handle_voice_stop()
  → asyncio.gather(ASR, VoiceprintProvider.identify_speaker())
  → {"content": ..., "speaker": ...}
  → receiveAudioHandle.startToChat()
  → conn.current_speaker
  → connection.py / dialogue.py 注入 <speakers_info> 到 system prompt
  → LLM function_call
  → unified_tool_handler / unified_tool_manager
  → 工具执行（无 speaker/owner 门控）
```

声纹介入点只有上述识别与提示词注入，以及默认关闭的 barge-in 打断。工具执行边界没有声纹介入。

## 3. 本轮修改与降级

| 项 | 修改前 | 本轮受控副本处理 |
| --- | --- | --- |
| barge-in owner 判断 | 仅比较 score；存在硬编码 speaker ID 兜底 | 删除兜底；必须配置 `barge_in_owner_ids` 且返回 ID 在 allowlist 内、分数达阈值、provider 健康才允许打断 |
| barge-in 作用 | 打断播放，但旧音频仍可能进 ASR | 只打开“可信拾音窗口”，不设置任何工具权限；旧音频 generation 失效 |
| 声纹服务不可用 | health 失败但 barge 探测仍可能发请求 | provider 未 enabled 时直接拒绝 barge-in |
| 自动注册 | 身份类短语命中后用首个 speaker ID 注册，可能覆盖声纹库 | 本轮未改业务语义；已加 generation 校验，旧音频不得触发覆盖；仍列为未解决风险 |

## 4. 安全降级结论

- 当前配置 `barge_in: false`，即使受控副本代码修正，生产真值仍是“声纹 barge-in 关闭”。
- 用户可用打断路径：设备本地唤醒词/按键通过 `listen start/detect` 文本显式打开可信拾音窗口；声纹只是可选增强，不是必要条件。
- 敏感操作授权：未实现。应作为后续独立 P0/P1，在 `UnifiedToolManager.execute_tool()` 或各 executor 前加入显式权限态、fail-closed、二次确认与覆盖测试；本轮未实施，也未宣称完成。

## 5. 测试覆盖状态

本轮新增 22 项测试覆盖门控、队列、TTS 状态、WebSocket 路由、`listen stop` generation 生命周期、显式 `None`/STREAM 旧回调、Abort 轮次隔离与门控原子性；不覆盖工具级声纹授权。视觉路由测试已随视觉功能从 R1 移除。生产 `tests/` 中仍无 `voiceprint`、`identify_speaker`、`enroll_speaker`、`current_speaker`、`speakers_info` 相关测试。
