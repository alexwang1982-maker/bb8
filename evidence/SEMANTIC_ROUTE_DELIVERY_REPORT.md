# BB-8 统一语义路由交付报告

## 范围与基线

- 分支：`feat/semantic-route`
- 基线：`4e9b98bcce6ea1239f10fba68e7297b9b25c817d`（R1 STREAM cleanup-race 最新提交）
- 未创建、未切换、未修改 `main`
- 未部署、未重启、未烧录、未触发任何真实硬件

本次只改三类文件：

1. `bridge/semantic_router.py`：新增纯语义路由决策模块。
2. `bridge/bridge.py`：在既有 `_handle_chat` 分派点做门控；复用原有工具与快通道，不注册新工具。
3. `server/tests/test_semantic_route.py` + `evidence/*`：回归用例与原始证据。

## 设计要点

完整用户表达 + 最小上下文 → 单一显式决策：

```
RouteDecision(intent, capability, parameters, confidence, reason, clarification, source)
```

- `intent`：`chat` / `vision` / `task` / `device_control` / `memory` / `music` / `local_fast` / `clarify` / `unresolvable`
- `capability`：已有能力或已有工具名（`vision_observe`、`ask_brain`、`forward_to_pc`、`self_eye_set` 等）
- `parameters`：由现有 detector / 原快通道产出，路由层不自行编造硬件参数
- `confidence`：确定性结果 0.58~0.97；模型结果必须 ≥0.75 才覆盖
- `clarification`：模糊、复合、裸指代、无上下文指代先问清，不执行

权限边界：

- 关键词快通道降级为「授权探测器」：只有当前表达实际命中的工具才进入 allowlist。
- 模型只在裸/无上下文视觉指代时被调用，且不带 tools；模型输出必须落在 allowlist 才可能生效。
- 设备动作仍走 bridge 原有工具调用链和小智侧 `plugins_func/register.py` 已注册工具；语义路由不新增硬件权限。
- 助手模式入口/退出及「模式内一律转发」状态机保持原顺序；语义澄清放在其后，不截断既有转发行为。
- 探测器阶段对 `local_fast` 的 `_last_robot` 写入做快照恢复，避免路由分析改变后续动作状态。

## 关键纠错

- `你看看这个是什么` + 视觉上下文 → `vision / vision_observe`，调用现有 `vision_observe()`。
- `我看看你能做什么` + 视觉上下文 → `chat / chat_llm`，不再因 `看看` 误触视觉。
- `你看看这个是什么` 无视觉上下文 → `clarify`，先要指代对象。
- `你看看前面有什么` / `你瞅瞅前面是啥` / `念一下屏幕上的字` → `vision`。
- `打开眼睛灯` → `device_control / self_eye_set`，不是视觉。
- `看着我` / `别跟了` → `device_control / self_behavior_set`。
- `摄像头的IP是多少` → `self_camera_send`；`摄像头识别结果是什么` → `self_camera_latest`，都不抓图。
- 模型伪造 `device_control/self_motor_drive` → 被 allowlist 拒绝，维持 `clarify`，且异常原因记录为 `invalid_model_route`。

完整逐例结果见 `evidence/SEMANTIC_ROUTE_KEY_CASES.txt`（22 PASS / 0 FAIL）。

## 测试证据

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| 语义路由专项 | `python -B -m unittest discover -s server/tests -p test_semantic_route.py -v` | `Ran 13 tests` / `OK` |
| R1 安全回归 | `python -B -m unittest tests.test_bb8_voice_safety_r1 -v`（xiaozhi-server venv2） | `Ran 35 tests` / `OK` |

原始输出：

- `evidence/SEMANTIC_ROUTE_TEST_OUTPUT.txt`
- `evidence/SEMANTIC_ROUTE_R1_REGRESSION_OUTPUT.txt`
- `evidence/SEMANTIC_ROUTE_KEY_CASES.txt`

专项测试覆盖：同关键词不同上下文、口语 ASR、复合请求、模糊请求、错误路由、任务/转发、记忆/音乐/本地快答、探测器无副作用、助手模式保持、模型输出异常（数组、非法参数、越界置信度、未知 intent、伪造设备工具、非 JSON）。

所有测试均为 mock；未连接真实摄像头、VLM、设备控制、ASR 云端或部署通道。

## 交付说明

- 全量补丁：`evidence/SEMANTIC_ROUTE_FULL_DIFF.patch`
- 工作副本哈希：`evidence/SEMANTIC_ROUTE_SHA256.csv`
- 最终提交 SHA：见交付回复或 `git log -1 --format=%H`（报告在提交前生成，避免自引用）
