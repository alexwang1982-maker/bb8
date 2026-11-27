# BB-8 真实环境连续多步骤任务测试 — 最终报告

日期：2026-11-27
分支：`feat/bb8-real-env-continuous-tasks`（基于 `origin/feat/vision-brain-context`）
生产只读；所有测试在隔离实例 `http://127.0.0.1:8910`、独立 `_isolated/tasks.db` 上进行。
真实 API：AGNES Brain、SiliconFlow Qwen3-VL、OpenMuse 8890、Hermes CLI。未刷 ESP32、未解除安全锁、未驱动电机。

## 轮次与结果

| 轮次 | 代码提交 | 结果 | 说明 |
|---|---|---|---|
| run1 基线 | 57a9406 | 1/7（C3 是弱判据假阳性；严格外部证据 0/7） | 无任务派发、无结果回灌、记忆注入关闭、快通道误判电机停止 |
| run2 | bc12910 | 5/7 | C4/C6 失败；引入 `--safe-mode` 导致 Hermes provider=auto 回归 |
| run3 | 0ea2ed6 | 2/7 | 技能名 `note-taking` 是目录不是技能；摄像头开始超时；C7 队列等待不足 |
| run4 最终 | 1db8082 | 3/7 | 真实技能已派发；剩余为外部摄像头故障与任务引擎假成功/编排断点 |

## 最终用例判定（run4，外部证据，不采信模型自述）

| 用例 | 结果 | 外部证据 | 事实 |
|---|---|---|---|
| C1 复杂意图 | 失败 | `run4/c1-complex-intent.raw.json`、`bridge.out.log`、无 `vision-response-*.json` | 摄像头抓图全部超时，模型如实回答“我现在看不清”；无物理工具泄漏 |
| C2 上下文+记忆 | **通过** | `run4/c2-context-memory.raw.json:memory_api`、`run4/agnes-request-001.json`、`bridge.out.log:memory -> AGNES context` | OpenMuse `/api/memory` 含标记 `BLUE-LANTERN-77`；AGNES 请求实际注入 `<openmuse_memory>`；回复精确返回该标记 |
| C3 任务拆解 | 失败（连续 2 次，已停止） | `run4/bridge.out.log:submit_task -> tsk_0133b18494004fc7`、`run4/c3-task-list.raw.json` | Brain 真实调用 submit_task（技能 `document-to-action-items`），但任务 `error.exit_code=-99`；判定时刻状态仍 running，任务后来被标记 succeeded 且带 error |
| C4 连续技能调度 | 失败（连续 2 次，已停止） | `run4/c4-task-list.raw.json`、`run4/c4-task-events.raw.json`、`run4/bridge.out.log` | 仅第一个任务 `tsk_f99aaf230e0e4e91` 被提交；第二个标记 `BB8-SKILL-TWO` 从未出现；第一个任务 exit_code=-99 |
| C5 视觉观察 | 失败 | `run4/c5-camera-frame.jpg`（空）、`run4/c5-vision.raw.json`、`bridge.out.log:capture ... timed out` | 三个摄像头地址（192.168.1.9/192.168.4.1/192.168.1.5）全部超时，真实帧字节数 0；未模拟画面 |
| C6 真实结果反馈+失败调整 | **通过** | `run4/c6-failure-submit.raw.json`、`c6-outbox.raw.json`、`task-result-*.json`、`bridge.out.log:task result -> Brain context`、`c6-adjusted-retry.raw.json` | 失败任务 outbox 已投递并注入 Brain；模型如实说明 exit_code=-99、主动询问是否换技能，并按用户同意提交了带 `BB8-ADJUSTED-OK` 的重试任务 |
| C7 重复执行/错误完成判定 | **通过** | `run4/c7-idempotency.raw.json`、`c7-judgement-status.raw.json`、`c7-completion-claim.raw.json` | 同一幂等键第二次返回 duplicate=true；真实状态 `unknown`，模型回答“没完成…status=unknown…exit_code -99”，未误报成功 |

## 已修复并经回归验证的断点

1. `bridge.py` 原本只有 `/tasks/*` 路由，没有把任务派发接进 Brain → 新增 `submit_task`/`check_task` 元工具，真实调用 `task_engine.submit/status/outbox`。
2. 任务结果 outbox 没有接收端 → 新增 `POST /tasks/result/receiver`、`GET /tasks/inbox`，下一轮对话把真实投递结果注入 `<task_engine_result>`。
3. `prepare_agnes_messages()` 硬关闭记忆注入 → 改为 `BB8_INJECT_OPENMUSE_MEMORY=1` 开关，并做 `<openmuse_memory>` 去重。
4. 快通道把“不要执行任何动作”误判为 `self_motor_stop` → 否定+动作只认严格位移词（已验证无物理工具泄漏）。
5. ASR 过滤器误杀含英文技能名/标记的任务句；60 字长句挡掉视觉请求 → 白名单扩展 + 长句显式视觉动词放行。
6. 我自己在 round1 引入的 `hermes --safe-mode` 回归（provider=auto 未配置）→ round2 移除，改用真实 provider `agnes/agnes-3.0-flash` + 技能白名单。
7. `note-taking` 等目录名不是可执行技能 → round3 映射到真实可用的 `document-to-action-items`，工具结果显式返回 `requested_skill`/`actual_hermes_skill`。
8. C7 判定快照过早（队列排在长任务后）→ 判定前重新读取 SQLite 真源，两个快照都留证。

## 未修复断点（3 轮修复上限，按规则停止）

1. **摄像头外部离线**：run4 时三个地址均超时；生产日志 16:37–17:08 曾真实抓到 4647/4517/5820/6381 字节画面。这不是代码问题，未模拟帧、未伪造观察。
2. **任务引擎假成功**：`error` 含 `exit_code=-99`（Hermes 异常退出/截断），但状态被写成 `succeeded`。需要让退出码参与终态判定；本轮未改。
3. **多步编排不等待**：Brain 在第一个任务为 `queued/running` 时停止，没有等待后提交第二步。需要真实的 wait/resume 编排；本轮未改。
4. **真实技能写笔记不稳定**：`document-to-action-items` 在真实文件工具上尝试写文件、`obsidian` 缺 vault 路径，导致 exit_code=-99。技能白名单已修，但落盘行为未改。

## 安全与脱敏

- 生产进程与生产 DB 只读观察，未修改；测试端口 8910、独立 DB、独立证据目录。
- API Key 仅在隔离进程环境变量中存在，未写入源码/DB/证据。
- OpenMuse 记忆 API 的原始响应里含历史遗留的 key 文本，提交前用 `tools/scrub_evidence.py` 替换为 `sk-REDACTED`（24+ 处）；结构、任务 ID、时间戳和模型回复保留。
- `.gitignore` 排除 `_isolated/`、`*.db`、`*.log`、`__pycache__`、会话文件；`post.token`、`server_token` 从未进入仓库。
- 全程未调用任何 `self_motor_*` / 固件 / 安全锁工具；run4 的 `physical_tool_leak` 为空。

## 证据位置

- `evidence/real-env-continuous/run1/` … `run4/`：每轮原始请求/响应、任务列表、outbox、AGNES 请求 dump、视觉响应 dump、摄像头帧、编排器输出、bridge 日志（本地日志被 `.gitignore` 排除，不上传）。
- `evidence/real-env-continuous/run4/SUMMARY.json`：最终机器可读判定。
- `tests/real_env_cases.py`：7 个用例及外部判据；`tools/restart_isolated.ps1`：隔离实例启动器；`tools/scrub_evidence.py`：证据脱敏。
