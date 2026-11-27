# BB-8 R1 密钥来源与暴露面审计（脱敏）

本文件只记录位置和状态，**不复制、不轮换、不迁移任何密钥值**。所有具体值均以 `<redacted>` 代替。只读检查，未修改生产配置或启动脚本。

## 1. 三路密钥来源

| 名称 | 当前唯一有效来源 | 位置 | 环境作用域注入 | 状态 |
| --- | --- | --- | --- | --- |
| `SF_VISION_KEY` | 源码内默认值 | `bridge/bridge.py:1092`（`os.environ.get("SF_VISION_KEY", "<redacted>")`） | Process/User/Machine 均 UNSET | 仅靠源码默认值运行 |
| `BRIDGE_LLM_KEY` | 启动脚本硬编码 | `G:\openmuse\xiaozhi-server\bridge\start_bridge.ps1:12`（deepseek 分支直接赋值 `<redacted>`） | User/Machine UNSET；仅在脚本启动的 Process 内注入 | 迁移前必须提供外部注入 |
| `VOICEPRINT_KEY` | 配置文件 URL 查询参数 | `G:\openmuse\xiaozhi-server\src\xiaozhi-esp32-server-main\main\xiaozhi-server\data\.config.yaml:25`（URL 内含 `key=<redacted>`） | Process/User/Machine 均 UNSET | 明文落盘；声纹服务当前未运行 |

`SF_VISION_MODEL`、`OPENMUSE_BRIDGE_TARGET`、`OPENMUSE_BRIDGE_TOKEN_FILE` 等非敏感配置项也集中在上述脚本，本文不逐项展开。

## 2. 环境注入实测

在本次执行会话中逐一读取三个作用域：

```
Name           Scope   State
SF_VISION_KEY  Process UNSET
SF_VISION_KEY  User    UNSET
SF_VISION_KEY  Machine UNSET
VOICEPRINT_KEY Process UNSET
VOICEPRINT_KEY User    UNSET
VOICEPRINT_KEY Machine UNSET
BRIDGE_LLM_KEY Process UNSET
BRIDGE_LLM_KEY User    UNSET
BRIDGE_LLM_KEY Machine UNSET
```

结论：**不存在可承接迁移的生产密钥注入配置**。

## 3. 部署门禁判定（BLOCKED）

根据任务单「密钥迁移缺少生产注入配置时停止部署并报告」：

- 若把 `SF_VISION_KEY` 从源码默认值移除并改为只读环境变量，而 User/Machine/Process 均无注入 → 视觉识别 (`vision_look`) 立即失效。
- 若把 `BRIDGE_LLM_KEY` 从 `start_bridge.ps1:12` 移除，而没有外部注入 → bridge 的 LLM 链路立即失效。
- `VOICEPRINT_KEY` 明文存于配置 URL，但当前 8010 无监听、无对应进程、`voiceprint_server_unavailable=24`，声纹本就不可用；即使轮换也不会恢复能力，反而可能引入配置漂移。

**因此：R1 不执行任何密钥移除/轮换/迁移，部署门禁维持 BLOCKED。** 本轮代码改动不含密钥相关变更，可在密钥保持现状的前提下独立审查。

## 4. 后续整改建议（不在本轮执行）

1. 引入受控密钥注入通道（例如 Windows 凭据管理器 / DPAPI 加密的 per-user 环境，或受 ACL 保护的 env 文件），并由 `start_bridge.ps1` 显式读取。
2. 将三路密钥的默认值改为 fail-closed：缺失注入时启动失败并输出明确错误，而不是用源码硬编码值继续运行。
3. 将 `VOICEPRINT_KEY` 从 URL 查询参数移出，改为 Authorization header + 环境变量；同步移除配置中的明文。
4. 轮换时按顺序：先建立注入通道并验证 → 再更新密文 → 再移除旧默认值 → 最后重启并冒烟。任何一步失败即回滚到上一步。
5. 上述步骤需单独评审与批准，不得并入 R1 音频门控部署。

## 5. 明确不做

- 不输出任何密钥片段、长度校验值以外的信息。
- 不调用任何外部密钥管理 API。
- 不修改 `.config.yaml` 或 `start_bridge.ps1`。
