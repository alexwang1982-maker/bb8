# BB-8 远端 Git 引用隐私清理报告

- 状态：**BLOCKED（按停止条件中止，未执行任何破坏性远端操作）**
- 目标仓库：`alexwang1982-maker/bb8`
- 干净交付分支：`feat/bb8-privacy-task-closure`
- 本报告提交：当前分支 HEAD（推送后以远端为准）
- 生成时间：2026-11-27T22:05:58+08:00

## 1. 停止原因

对远端全部可见 refs 的只读扫描发现：`main` 可达历史命中 3 处（`BB8-interface-extraction-2026-11-27-redacted.zip`：`private_fact` x1、`session_id` x2，引入 commit `563ff60472c8`）。干净交付分支 `feat/bb8-privacy-task-closure` 为 0 命中。

按执行要求中的停止条件“如果发现 `main` 或干净交付分支也含敏感信息，立即停止破坏性操作并报告”，本次**未删除任何远端分支、未重写任何历史、未触碰 `main`**。因此可控 refs 的清理没有继续执行，不能宣称远端已清理完成。

## 2. 清理前远端 refs 清单

通过 GitHub API 与 `git ls-remote` 双重获取；`HEAD` 符号指向 `main`。tags=0；PRs=0；显式 `refs/pull/*/head` 与 `refs/pull/*/merge` 查询均为空。

| 类型 | ref | SHA | 保护 |
| --- | --- | --- | --- |
| branch | `refs/heads/feat/bb8-privacy-task-closure` | `610c3c5f773750fc2a0c8e23cc2d2c65f6c33849` | false |
| branch | `refs/heads/feat/bb8-real-env-continuous-tasks` | `5b4135e0fca033f0389eb5befbc76c433df00451` | false |
| branch | `refs/heads/feat/bb8-task-closure-round4` | `3e1ddc881cd6e1e7d9eaa765610dc1b314959e13` | false |
| branch | `refs/heads/feat/vision-brain-context` | `17a090be540d1cfe713c79bc19c1b19712920260` | false |
| branch | `refs/heads/feat/vision-brain-real-env-20261127` | `e1dfe520303bce2fba92e450da790c95a5b79356` | false |
| branch | `refs/heads/main` | `563ff60472c8351b756b6563bfa36913bfe18c3e` | false |
| symbolic | `HEAD` | `563ff60472c8351b756b6563bfa36913bfe18c3e` | -> main |

## 3. 扫描范围与方法

- 在临时目录创建完整镜像并显式 fetch PR refspec；扫描器：`tools/privacy_scan_remote.py`。
- 对每个 ref 执行 `git rev-list --objects <ref>`，仅对 blob 运行通用隐私规则；规则定义与合成夹具文件被跳过。
- 报告只记录规则名、路径、计数和 commit SHA；**不记录、不复制任何原始命中文本**。
- 报告推送后复核扫描总计：6 个 refs，860 个 blob 对象，860 条路径/规则行，976 处命中。

| ref | SHA | 对象数 | 命中行 | 命中数 |
| --- | --- | ---: | ---: | ---: |
| `refs/heads/feat/bb8-privacy-task-closure` | `c3e23c5d7a11` | 361 | 0 | 0 |
| `refs/heads/feat/bb8-real-env-continuous-tasks` | `5b4135e0fca0` | 172 | 290 | 346 |
| `refs/heads/feat/bb8-task-closure-round4` | `3e1ddc881cd6` | 295 | 564 | 621 |
| `refs/heads/feat/vision-brain-context` | `17a090be540d` | 11 | 2 | 3 |
| `refs/heads/feat/vision-brain-real-env-20261127` | `e1dfe520303b` | 19 | 2 | 3 |
| `refs/heads/main` | `563ff60472c8` | 2 | 2 | 3 |

## 4. 泄露文件定位（路径 / ref / commit SHA）

共 141 个唯一路径。下表按命中数降序；“引入 commit”取该 ref 中该路径最早触及的 commit。`命中` 为规则命中次数，不等于行数。

| 文件 | 规则 | 命中 | 所在 ref | 引入 commit |
| --- | --- | ---: | --- | --- |
| `evidence/real-env-continuous/run1/c2-context-memory.raw.json` | personal_name, private_address_form, private_fact, private_memory_json, test_password | 32 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c2-context-memory.raw.json` | personal_name, private_address_form, private_fact, private_memory_json, test_password | 32 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c2-context-memory.raw.json` | personal_name, private_address_form, private_fact, private_memory_json, test_password | 32 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c2-context-memory.raw.json` | personal_name, private_address_form, private_fact, private_memory_json, test_password | 32 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `BB8-interface-extraction-2026-11-27-redacted.zip` | private_fact, session_id | 15 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4`, `feat/vision-brain-context`, `feat/vision-brain-real-env-20261127`, `main` | `563ff60472c8` |
| `evidence/real-env-continuous/run2/agnes-request-004.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 14 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 14 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-009.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 14 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-003.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-005.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-006.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-007.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-008.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-009.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-010.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-011.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-012.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/agnes-request-013.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-003.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-004.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-005.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-006.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-007.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-008.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-009.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-010.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-011.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-012.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-013.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-014.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/agnes-request-015.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-003.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-004.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-005.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-006.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-007.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-008.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-010.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-011.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-012.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/agnes-request-013.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `tests/real_env_cases.py` | private_memory_json | 12 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `bc129101e213` |
| `bridge/bridge.py` | private_fact | 9 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `57a94068fb99` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-003.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-004.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-005.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-006.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-007.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-008.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-009.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-010.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-011.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/agnes-request-012.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-003.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-004.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-005.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-006.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-007.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-008.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-009.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-010.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-011.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-012.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/agnes-request-013.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-003.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-004.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-005.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-006.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-007.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-008.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-009.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-010.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-011.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-012.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/agnes-request-013.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_t6/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `3e1ddc881cd6` |
| `evidence/real-env-continuous/round4_t6/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `3e1ddc881cd6` |
| `evidence/real-env-continuous/round4_t6b/agnes-request-001.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `3e1ddc881cd6` |
| `evidence/real-env-continuous/round4_t6b/agnes-request-002.json` | personal_name, private_address_form, private_fact, private_memory_block, private_memory_json, test_password | 6 | `feat/bb8-task-closure-round4` | `3e1ddc881cd6` |
| `bridge/forward_target.txt` | session_id | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `57a94068fb99` |
| `bridge/opencode_forward.py` | session_id | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `57a94068fb99` |
| `evidence/real-env-continuous/run1/c1-complex-intent.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run1/c3-decomposition.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run1/c4-skill-sequence.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run1/c5-vision.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run1/c6-adjusted-retry.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run1/c6-feedback-adjust.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run1/c7-completion-claim.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c1-complex-intent.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c3-decomposition.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c4-skill-sequence.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c5-vision.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c6-adjusted-retry.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c6-feedback-adjust.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run2/c7-completion-claim.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c1-complex-intent.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c3-decomposition.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c4-skill-sequence.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c5-vision.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c6-adjusted-retry.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c6-feedback-adjust.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run3/c7-completion-claim.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c1-complex-intent.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c3-decomposition.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c4-skill-sequence.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c4-task-list.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c5-vision.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c6-adjusted-retry.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c6-feedback-adjust.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c6-task-list.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/run4/c7-completion-claim.raw.json` | private_memory_json | 2 | `feat/bb8-real-env-continuous-tasks`, `feat/bb8-task-closure-round4` | `5b4135e0fca0` |
| `evidence/real-env-continuous/round4_r2/t4-failed-task.raw.json` | personal_name, private_memory_json | 2 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/t1-single-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/t2-failure-brain.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/t3-seq2-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/t4-adjusted-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/t4-failed-task.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r1c/t4-skip-success.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/SUMMARY.json` | personal_name | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/t1-single-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/t2-failure-brain.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/t3-seq2-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/t4-adjusted-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r2/t4-skip-success.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/t1-single-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/t2-failure-brain.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/t3-seq2-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/t4-adjusted-submit.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/t4-failed-task.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_r3/t4-skip-success.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `638a3b06df48` |
| `evidence/real-env-continuous/round4_t6/t6-brain.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `3e1ddc881cd6` |
| `evidence/real-env-continuous/round4_t6b/t6-brain.raw.json` | private_memory_json | 1 | `feat/bb8-task-closure-round4` | `3e1ddc881cd6` |

## 5. 清理与重写记录

- 删除远端分支：**0**（因停止条件触发，未执行）。
- 重写远端历史：**0**（因停止条件触发，未执行）。
- 标签清理：**0**（无 tags）。
- PR ref 清理：**0**（无 PRs，且 GitHub 不允许直接删除 PR refs；本次未遇到该限制的实际对象）。
- 干净交付分支：`feat/bb8-privacy-task-closure` 未被删除、未被重写；原 T6 两次失败记录与修复后专项验证证据均保留。

## 6. 清理后复核（实际远端查询）

因未执行清理，复核后 refs 与清理前一致：6 branches、0 tags、0 PRs；`main` 仍为 `563ff60472c8`。干净交付分支为本报告最终 HEAD，见远端实际值。

| 类型 | ref | SHA |
| --- | --- | --- |
| branch | `refs/heads/feat/bb8-privacy-task-closure` | `见远端（本报告最终 HEAD）` |
| branch | `refs/heads/feat/bb8-real-env-continuous-tasks` | `5b4135e0fca033f0389eb5befbc76c433df00451` |
| branch | `refs/heads/feat/bb8-task-closure-round4` | `3e1ddc881cd6e1e7d9eaa765610dc1b314959e13` |
| branch | `refs/heads/feat/vision-brain-context` | `17a090be540d1cfe713c79bc19c1b19712920260` |
| branch | `refs/heads/feat/vision-brain-real-env-20261127` | `e1dfe520303bce2fba92e450da790c95a5b79356` |
| branch | `refs/heads/main` | `563ff60472c8351b756b6563bfa36913bfe18c3e` |

报告推送后已重新镜像并全量扫描：6 refs、860 个 blob 对象、860 条命中行、976 处命中；干净交付分支最终 HEAD 仍为 0 命中，其余 refs 命中数与清理前一致。

## 7. 剩余风险与限制

- **BLOCKED**：`main` 仍可达含敏感模式的 ZIP 历史对象；按安全边界不得修改 `main`，故无法清理。
- 4 个旧 feature 分支仍可达原始证据；因停止条件优先，未执行删除。
- GitHub 内部对象缓存、PR refs（本次为空）以及 fork 网络不在可控删除范围内；不能宣称这些对象已被物理删除。
- 未把私人记忆、凭据或原始命中文本写入本报告或任何日志。

## 8. 结论

**BLOCKED。** 停止条件已触发，未进行任何远端破坏性操作。干净交付分支保持完整且 0 命中；但 `main` 存在 3 处命中，且旧污染分支仍在远端可见。只有在明确授权修改 `main` 并重新定义验收口径后，才能继续清理。
