# BB-8 隐私清理与任务判定闭环最终报告

- 分支：`feat/bb8-privacy-task-closure`（从 `3e1ddc8` 派生，未改 `main`，未改生产实例）
- 最终提交：当前分支 HEAD（push 后以远端分支为准）
- 隔离实例：`http://127.0.0.1:8910`，独立 `G:\bb8-real-env-test\_isolated\tasks.db`，真实 AGNES key / OpenMuse token / Hermes CLI / 非物理 Skill
- 判定口径：只采信 Task Engine SQLite/HTTP、`/tasks/verify`、`result_outbox`、`task_events`、bridge 日志与 AGNES 请求 dump；模型自述不作为完成证据

## 1. 隐私清理结果

| 项目 | 结果 |
| --- | --- |
| 首次全量扫描（当前树 + HEAD 历史） | 1675 处命中，覆盖 357 个历史 blob / 287 个跟踪文件 / 292 个工作区文件 |
| 清理范围 | 整个 `evidence/`，以及 ZIP 内文本成员、`bridge/`、报告、测试夹具 |
| 历史重写 | 仅重写本新分支全部 12 个可达提交；`main` / `origin/main` 仍指向 `563ff604` |
| 重写后 `refs/original` | 已删除，旧敏感历史不再被本仓库引用 |
| 最终全量扫描 | `history_objects + tree_files + worktree_files` 全部 0 命中 |
| 扫描报告 | `privacy_scan --mode all --json D:\Temp\opencode\privacy-scan-final-all.json`（434 个历史 blob / 349 个 tree 文件 / 344 个工作区文件） |

规则代码本身不含任何真实姓名、口令、key 或原始记忆；它只使用通用模式（凭据形状、记忆容器、泛化隐私句式）。规则定义与其合成回归夹具被扫描器明确跳过，避免把假样本误判为残留。

## 2. 判定与编排修复

1. `verified_status()` 强制要求有效 `expected_marker`：缺失、空白、错误标记均 `verified=false`；原四重校验（`succeeded`、`error` 空、`exit_code=0`、标记命中真实 stdout）全部保留。
2. `/tasks/submit` 与 `submit_task` 工具入口拒绝缺失/空白 `expected_marker` 和缺失 `step_id`；提交时把标记写入任务 payload。
3. 执行器也使用同一严格判定：即使 `rc==0`，没有标记或标记不命中也进入失败/重试，不会写入成功态。
4. `depends_on` 支持任务 ID 或步骤 ID；只有依赖进入真实终态且四重校验通过才提交下一步。前置失败返回 `dependency_failed_verification` 且不创建任务。
5. 已验证成功的步骤通过现有 `tasks.step_id` 跳过，不新建 step ledger、不新增加总编排器；幂等键与 outbox 复用原表。

## 3. 回归测试

`python -m unittest discover -s tests -p 'test_*guard.py'`：**9/9 通过**。

覆盖：缺失/空白标记提交被拒、缺失 `step_id` 被拒、正确标记通过、错误/空白校验标记失败、无标记遗留任务失败、`marker_missing()` 严格性、错误标记不跳过成功步骤、记忆/凭据脱敏幂等、ZIP 成员脱敏。

## 4. 隔离实例真实验收（真实 API + Hermes CLI + 非物理 Skill）

部署：`restart_isolated.ps1 -RunTag privacy_r1 -Fresh`；health 确认 DB 为 `_isolated\tasks.db`、receiver 为 8910；隔离副本 `task_engine.py` 含严格标记代码。

| 用例 | 结果 | 外部证据 / 观察 |
| --- | --- | --- |
| T0 真实 HTTP 标记守卫 | PASS | 缺失/空白标记提交均 400，缺失 step_id 400；正确标记 `verified=true`；错误标记 `marker_missing:...`；空白标记 `expected_marker_missing_or_blank` |
| T1 单任务成功 | PASS | `tsk_65b1acefd38b4bbb`，`succeeded`、`exit_code=0`、标记命中、日志命中 |
| T2 单任务失败回灌 | PASS | `tsk_e72b3078080d492a` 真实技能解析失败，`failed`、`exit_code=1`，outbox 存在，Brain 如实报告失败 |
| T3 两步等待序列 | PASS | `tsk_6cb3c933b40244c7` → `tsk_24fa97ac2287499b`；`finished_at <= created_at`，依赖校验日志命中，两步均 verified |
| T4 失败调整 + 成功步骤跳过 | PASS | 失败 `tsk_448c675fa6fa473c`；已成功步骤 SKIP 且任务数仍为 1；修正步 `tsk_af73350a46f44869` 在成功后创建并 verified |
| T5 幂等重复提交 | PASS | `tsk_ceb1d5119cb94577`，第二次 `duplicate=true`，同 step 仅 1 条任务且 verified |
| T6 outbox 注入 Brain | PASS | `tsk_ca528eb266cf4132`；`delivered_before_chat=1`；AGNES dump 含 `<task_engine_result>` 和 task ID；bridge 日志 `task result -> Brain context` |
| T7 前置失败阻断 | PASS | `tsk_3b6a062970f24912` 失败；依赖它的步骤返回 `dependency_failed_verification`，未创建任务 |

原始证据目录：`evidence/real-env-continuous/privacy_r1/`、`privacy_r2/`、`privacy_r3/`。所有新证据已再次脱敏并通过最终扫描。

### T6 专项失败记录保留

`round4_t6` 与 `round4_t6b` 两次失败证据原样保留，未改写。原因是测试夹具在异步 outbox 投递前发起 Brain 轮次。修复夹具为“提交后先等 `delivered=1` 再发起 Brain 轮次”后，只做了一次新的专项真实验证：`privacy_r1` T6 PASS；`privacy_r3` T7 PASS。按规则未对旧失败做第三次重试。

## 5. Git 统计差异说明

本地原始比较（`5b4135e0..3e1ddc8`，重写前）：`123 files changed, 9022 insertions(+), 40 deletions(-)`。

GitHub compare API 对同一比较返回：`123 files, 7998 additions, 39 deletions, 2 commits`。差额为 `+1024/-1`，不是代码差异：API 对 12 个文件返回了 `additions=0/deletions=0`（大 patch 被省略），而本地 `git diff --numstat` 完整计数。这 12 个文件合计正好 1024 行新增、1 行删除：

`round4_t6/t6-outbox.raw.json`、`round4_t6/task-result-001.json`、`round4_t6b/SUMMARY.json`、`round4_t6b/agnes-request-001.json`、`round4_t6b/agnes-request-002.json`、`round4_t6b/harness.out.txt`、`round4_t6b/t6-brain.raw.json`、`round4_t6b/t6-injection.raw.json`、`round4_t6b/t6-outbox.raw.json`、`round4_t6b/task-result-001.json`、`tests/task_closure_cases.py`、`tools/restart_isolated.ps1`。

因此本地 Git 数字为权威值；GitHub 页面/API 数字仅在未被截断的文件上求和，不能作为最终统计。

本分支重写后相对重写基线 `6a3bc66` 的最终差异：`189 files changed, 14750 insertions(+), 78 deletions(-)`。

提交列表（`6a3bc66..HEAD`）：

1. 当前 HEAD BB-8 privacy/task closure: real T0/T7 probes, scrubbed evidence, final report
2. `2de88ff` BB-8 privacy/task closure: strict markers, dependency guards, full evidence scrub
3. `7944937` BB-8 round4: add T6 outbox reinjection probe evidence and stop-rule report
4. `a0184e2` BB-8 round4: strict task terminal verification, wait-style orchestration, skip verified steps (5/5 real-env)

## 6. 最终结论

**通过。** 隐私全量扫描 0 残留；严格标记无法绕过；依赖失败确实阻断；成功步骤不重做；隔离实例真实 API/Hermes/Skill 端到端 8 个场景全部通过；旧 T6 失败证据保留且未被改写。未执行任何物理动作，未改生产实例或 `main`。
