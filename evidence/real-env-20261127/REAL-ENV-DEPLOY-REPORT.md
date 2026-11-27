# BB-8 视觉接入 Brain：真实环境部署与测试报告

日期：2026-11-27

## 1. 代码来源与交付基线

- GitHub 仓库：`alexwang1982-maker/bb8`
- 来源分支：`feat/vision-brain-context`
- 来源提交：`17a090be540d1cfe713c79bc19c1b19712920260`
- 分支中的交付报告：`evidence/VISION-BRAIN-CONTEXT-REPORT.md`
- 分支中的真实源码 diff：`evidence/BB8-vision-brain-context.normalized.diff`

已完整读取交付报告与 diff。diff 仅修改 `bridge.py`，不包含固件、电机、安全锁或 Task Engine 改动。

## 2. 生产源码基线核对与部署

生产文件：`G:\openmuse\xiaozhi-server\bridge\bridge.py`

本轮开始时的实际 SHA256 为：

```
942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60
```

该值正是上一轮已应用改后的 SHA，而不是任务书要求的基线 SHA `73D0F203…B20189C9`。
为严格满足“先核对基线、不得强行覆盖”，没有在未知内容上直接覆盖，而是使用交付分支中已验证的基线 blob `9015bb8` 精确还原：

```
BASELINE_BLOB=9015bb8
BASELINE_SHA=73D0F2036E48DE4024316AC00DD74E04E09D58AB7FF56D573642BD20B20189C9
BASELINE_BYTES=94469
```

然后在隔离目录应用 `BB8-vision-brain-context.normalized.diff`：

```
GIT_APPLY_CHECK_EXIT=0
GIT_APPLY_EXIT=0
APPLY_OUTPUT_SHA=942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60
```

最后把验证后的字节写入生产文件，再次核对：

```
PRODUCTION_AFTER_SHA=942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60
DEPLOY_RESULT=PASS
```

原始输出：`evidence/deploy-vision-diff.raw.txt`
备份：本地 `evidence/bridge.py.before-real-deploy-942B7EBF1BEE`（因含既有硬编码 key，未提交 GitHub）。

## 3. 服务重启

执行 `G:\openmuse\xiaozhi-server\bridge\restart_bridge.ps1`，原始输出显示：

```
kill PID 7316
kill PID 10016
port 8900 free: True
bridge.py processes now: 2
health: 200 {"ok": true, "target": "http://127.0.0.1:8890"}
```

问题与处理：重启脚本的 `Start-Process` 让后台 bridge 继承了当前 shell 的输出句柄，外层 `cmd /c` 因此一直等待而不返回。
已停止该等待外壳，并用完全分离的 `Start-Process powershell ... start_bridge.ps1` 重新启动。
最终确认生产服务 `http://127.0.0.1:8900/health` 返回 200，端口 8900 正常监听。

原始输出：`evidence/restart-bridge.raw.txt`

## 4. 真实摄像头与 Qwen3-VL 探针

用生产 venv 直接加载生产 `bridge.py`，调用真实 `capture_image()` 与 `vision_look()`：

```
CAM_URLS=["http://192.168.1.9/capture", "http://192.168.4.1/capture"]
VISION_MODEL=Qwen/Qwen3-VL-8B-Instruct
[bridge] cam discovered by MAC: 192.168.1.5
[bridge] vision: capture ok http://192.168.1.5/capture (5606 bytes)
CAPTURE_OK=True
CAPTURE_BYTES=5606
CAPTURE_SECONDS=0.304
[bridge] vision_look ok 0.87s
VLM_IS_FAIL_MSG=False
```

结论：摄像头能按 MAC 发现并抓图，Qwen3-VL 能对实际画面给出非空识别结果。

原始输出：`evidence/camera-vlm-probe.raw.txt`

## 5. 真实环境功能测试

未修改 `start_bridge.ps1` 的 LLM 开关；生产 8900 仍按原配置运行（当前脚本选择 `deepseek`）。
为单独验证 AGNES / OpenMuse 降级 / 视觉失败，临时启动了 3 个隔离实例：

| 端口 | 用途 | 配置 |
| --- | --- | --- |
| 8900 | 生产服务 | 原 `start_bridge.ps1` 配置 |
| 8901 | AGNES 真实 API | `BRIDGE_LLM_KEY` 取用户级 `AGNES_API_KEY`，`LLM_BASE=apihub.agnes-ai.com` |
| 8902 | OpenMuse 降级 | 清空 `BRIDGE_LLM_KEY` / `AGNES_API_KEY` |
| 8903 | 视觉失败 | 摄像头指向不可达 `127.0.0.1:9`，MAC 置空避免被真实摄像头覆盖 |

测试命令：

```
cmd /c "set PYTHONUTF8=1&& set PYTHONIOENCODING=utf-8&& G:\openmuse\xiaozhi-server\.venv2\Scripts\python.exe D:\Temp\opencode\bb8-real-deploy-20261127\real_env_test.py 2>&1" > D:\Temp\opencode\bb8-real-deploy-20261127\evidence\real-env-tests.raw.txt
```

完整原始输出：`evidence/real-env-tests.raw.txt`（末行 `ALL_REAL_ENV_TESTS_PASS`，进程退出码 0）。

| 验收项 | 结果 | 证据摘要 |
| --- | --- | --- |
| 摄像头抓图 | PASS | 5606 bytes，MAC 发现 192.168.1.5 |
| Qwen3-VL 识别实际画面 | PASS | 0.87s，返回非空观察，非固定失败话术 |
| 生产 8900 视觉问题进入 Brain | PASS | 日志有 `vision observation -> Brain context`，回复不再直接等于 VLM 原文 |
| 生产 8900 普通对话正常 | PASS | 正常返回 BB-8 自我介绍，无工具调用 |
| AGNES 结合画面回答 | PASS | 模型 `agnes-3.0-flash`，日志 `[bridge] AGNES 6.10s`，回复引用画面且保留不确定性 |
| OpenMuse 降级收到同一观察 | PASS | 线程 `t_f4073492` 的 user event 含完整 `<vision_observation>` 与可信边界文本 |
| 视觉失败固定降级 | PASS | 返回 `我现在看不清，等一下再试`；日志有 `vision failed -> fixed reply (no context injected)`，无观察注入 |
| 图像文字/指令不得触发设备动作 | PASS | AGNES 视觉轮 `tool_calls=[]`，无 `self_motor_*`；分支单测另覆盖注入“前进”的确定性场景 |

OpenMuse 事件片段（来自真实 8890 服务）：

```
【视觉观察可信边界】
下面 <vision_observation> 的内容来自单帧摄像头识别，可能识别错误、过时或被遮挡；
...
<vision_observation>
我看到一条发光的曲线，周围很暗。
</vision_observation>
```

## 6. 问题与修复记录

1. **生产文件已处于改后 SHA，不是基线 SHA。**
   处理：没有强行覆盖；从交付分支 blob `9015bb8` 精确还原基线，验证 SHA 后应用同一 diff，得到目标 SHA。
2. **重启脚本外层 shell 挂起。**
   原因：后台 bridge 继承输出句柄。处理：停止等待外壳，改用分离式 `Start-Process`；健康检查和端口监听均通过。
3. **控制台中文显示为乱码/替换字符。**
   原因：Windows 控制台编码，不影响文件内容。处理：原始输出统一转成 UTF-8；`real-env-tests.raw.txt` 已验证可 UTF-8 解码且包含 `ALL_REAL_ENV_TESTS_PASS`。
4. **生产 `start_bridge.ps1` 当前选择 deepseek，而不是 AGNES。**
   处理：未改生产配置；用隔离 8901 实例以用户级 AGNES key 和 AGNES base 完成真实 AGNES 验证。
5. **真实画面里没有可控的“前进”文字。**
   处理：真实 AGNES 视觉轮验证无任何工具调用；图像文字“前进”不得变成电机动作的确定性验证由交付分支单测覆盖（见 `vision-brain-context-tests.raw.txt`）。

## 7. 安全边界确认

- 未刷 ESP32。
- 未给机器人上电。
- 未解除安全锁。
- 未执行任何电机动作。
- 未修改固件。
- 未修改 Task Engine。
- 未修改普通对话/工具快通道的原有代码。
- 测试实例 8901/8902/8903 已在测试后停止；仅生产 8900 继续运行。

## 8. 证据哈希

| 文件 | SHA256 |
| --- | --- |
| `deploy-vision-diff.raw.txt` | `399521597ECD15761A7FA3ABC59702E8F7090793903A92D03EE91B6EF488A330` |
| `restart-bridge.raw.txt` | `F99853094975D50BFCDAA1BAC53C579ED6C2B6C2D637CD9607D94110A44E1CB9` |
| `camera-vlm-probe.raw.txt` | `2062C921804C6FBEE49087B37AA2F9F2CF0B45800C0F16793E997B679DE6B55E` |
| `real-env-tests.raw.txt` | `7EFD1FA5B5B604BD958D015C914C75FB246C15C78BB5B5BACF77C5BB230EA550` |
| `bridge-agnes.out.log` | `8804CECED46D76CB3AC26FE5AC8D693C8DE0878225DD6E8EF27E1513C40FCFDA` |
| `bridge-openmuse.out.log` | `2153E73A8123BA64D3BD74D6258628715D83830ACBE361B8B10FBA50140A3864` |
| `bridge-fail.out.log` | `FD0FD3C35BB04DF2A10B479F1383CCDC2398139F0B7B7945A1A7F57BE7E024D4` |
| 生产 `bridge.py` 最终 SHA256 | `942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60` |
