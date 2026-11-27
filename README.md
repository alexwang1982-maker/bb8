# BB-8 大脑接入接口提取材料（2026-11-27）

本仓库仅存放**只读提取的接口材料**，不含运行服务、不含固件、未修改任何源码。

## 文件

- `BB8-interface-extraction-2026-11-27-redacted.zip`
  - SHA-256：`5CCC8CED83037244CBF1D34BFB1E3A17C5DB0D811AE7822BC8975696DF8F988E`
  - 内含完整源码文件（非片段）、`00-MANIFEST.txt`（绝对路径、字节数、SHA-256）、`00-INTERFACE-SUMMARY.md`、`00-BRAIN-ABSENCE.txt`、`00-CAMERA-EVIDENCE.txt`。

## 脱敏说明（重要）

本仓库是公开仓库，因此上传副本做了两处**凭据脱敏**，本机原始源码未改动：

- `bridge/bridge.py`：`SF_VISION_KEY` 默认值置空，运行时须设置环境变量 `SF_VISION_KEY`。
- `bridge/start_bridge.ps1`：`BRIDGE_LLM_KEY` 硬编码值置空，运行时须设置环境变量 `BRIDGE_LLM_KEY`。

因此脱敏副本中的 `bridge.py` / `start_bridge.ps1` 哈希与本机原文件不同；其余文件与本机原文件逐字节一致。清单内记录的是**上传副本**的哈希。

## 关键结论

- 当前运行项目：`G:\openmuse\xiaozhi-server\`；`bridge.py` / `task_engine.py` 在 `bridge/` 子目录。
- 本地 `brain.py` 与 `brain/__init__.py` **不存在**；Brain 实际是 `bridge.py` 的 `ask_brain` 工具 + OpenMuse HTTP API。
- 视觉模型**已接入**：SiliconFlow `Qwen/Qwen3-VL-8B-Instruct`，完整实现 `vision_look()`。
- `self.camera.latest` 固件源码不在本机，接口返回形态与证据见 `00-CAMERA-EVIDENCE.txt`；完整固件实现**无法确认**。
- `self.status` 字段契约见 `core/bb8_safe_test.py:54-98`。
- Task Engine 入口 `task_engine.handle_request()`，路由 `/tasks/*`，状态在 SQLite `tasks.db`。
- 相关目录均非 Git 仓库，故原项目无 commit / 工作区状态可提供。
