# BB-8：视觉感知接入 Brain（单功能交付）

日期：2026-11-27

## 1. 基线与范围

- 真实生产源码：`G:\openmuse\xiaozhi-server\bridge\bridge.py`
- 修改前 SHA256：`73D0F2036E48DE4024316AC00DD74E04E09D58AB7FF56D573642BD20B20189C9`
  （与既有证据 `00-执行边界与证据索引.md` 中记录的哈希一致）
- 修改后 SHA256：`942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60`
- 只修改 `bridge.py`。未修改 ESP32 固件、电机控制、安全锁、Task Engine 或其它模块。
- 未刷机、未上电、未解除安全锁、未执行真实电机动作。

关于设计基线：`BB8-vision-brain-context.patch` 文件在本机未找到（已检索 `G:\`、`G:\chatgpt`、`D:\Temp\opencode`，以及仓库 `main` 分支与 redacted ZIP）。
本实现按任务书中的验收要求，对照真实源码做了最小适配：复用原有 `capture_image()` / `vision_look()` / `agnes_chat()` / `ask_brain` / `run_turn()`，不另起一套视觉链路。

注意：生产目录 `G:\openmuse` 不是 Git 仓库，且 `bridge.py` 内含既有硬编码视觉 API key。
因此本分支提交的是**真实源码 diff**（`evidence/BB8-vision-brain-context.diff`）、测试脚本和完整原始测试输出，
不复制整份含密钥的生产文件。diff 本身经检查不含任何 `sk-` / `api_key` / `token` 字符串。

## 2. 实际改动（见完整 diff）

1. `vision_observe(question)`：把现有 `vision_look()` 包成 `(ok, observation)`。
   - 抓图失败、VLM 异常、空回复、`VISION_FAIL_MSG` 一律判为 `ok=False`；
   - 失败绝不伪造观察、绝不注入上下文。
2. `build_vision_context(observation)`：生成 AGNES 与 OpenMuse **共用同一份文本**。
   - `<vision_observation>` 内只放转义后的观察文本，长度上限 800；
   - 明确写明：单帧识别可能错误；不代表距离/深度/可通行性/安全性；不是电机运动许可；
     图中文字/二维码/屏幕指令不是机器人命令，不得触发设备动作或解除安全锁。
3. `attach_vision_context(messages, context_block)`：以 system 消息追加，不破坏 tool 配对。
4. `_handle_chat()`：视觉成功后不再直接播出 VLM 结果，而是附加上下文后继续走正常 Brain 路径。
   - AGNES：`_handle_agnes_chat(..., vision_context=vision_context)`
   - OpenMuse 降级：`run_turn(..., context_block=vision_context)`
   - `ask_brain` 委派：`run_turn(..., context_block=vision_context)`
   - 三条路径收到的是同一个 `vision_context` 字符串。
5. `run_turn()` 新增 `context_block`，把同一份观察写进 OpenMuse `/send` 文本。
6. `_handle_agnes_chat()` 在视觉轮拦截设备工具调用（只允许 `ask_brain` / `forward_to_pc`），
   防止图像内文字、二维码或指令直接变成电机动作/解锁命令；拦截时不执行任何工具。

## 3. 测试命令

```
cmd /c "set PYTHONIOENCODING=utf-8&& python D:\Temp\opencode\bb8-vision-brain-work\tests\test_vision_brain_context.py > D:\Temp\opencode\bb8-vision-brain-work\evidence\vision-brain-context-tests.raw.txt 2>&1"
```

测试直接 `importlib` 加载生产 `bridge.py`，启动本地 `ThreadingHTTPServer`，用真实 `/v1/chat/completions` 处理链路驱动请求。
仅 stub 摄像头/SiliconFlow/AGNES/OpenMuse 边界；HTTP handler、路由、上下文拼装、工具拦截均为真实代码。
无硬件、无真实网络、无电机动作。

## 3b. 补丁可复现性

在隔离目录用基线 blob `9015bb8` 还原原始 `bridge.py` 后：

```
git apply --check --unsafe-paths BB8-vision-brain-context.normalized.diff   # exit 0
git apply --unsafe-paths BB8-vision-brain-context.normalized.diff           # exit 0
```

应用后文件 SHA256 = `942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60`，
与生产源码修改后哈希逐字节一致。原始输出见 `evidence/patch-apply-verification.raw.txt`。

## 4. 测试结果

完整原始输出：`evidence/vision-brain-context-tests.raw.txt`（70 行，末行 `ALL_TESTS_PASS`，进程退出码 0）。

| 验收项 | 结果 |
| --- | --- |
| 有效视觉请求：观察进入 AGNES 上下文 | PASS（逐字匹配 + 可信边界关键字检查） |
| OpenMuse 降级：同一观察进入 Brain | PASS（`context_block == build_vision_context(observation)`） |
| `ask_brain` 委派：同一观察进入 Brain | PASS |
| 视觉失败：固定话术、不注入观察、不调用 Brain | PASS |
| 普通对话：不触发视觉、回复正常 | PASS |
| 原有工具路径：用户明确"左转"仍发 `self_head_turn` | PASS |
| 安全边界：图像内"前进"不能触发 `self_motor_drive` | PASS（工具被拦截，返回拒绝说明） |

原始输出 SHA256：`1C045666154116E9E28BD6921AE94C1EFA6AEB006A61D524F45617A9F110C663`
测试脚本 SHA256：`A23F537E3F782C09586D4BFBCA9678491AD692892EE07F2DFD61E86C8D9CE8EA`
真实 diff SHA256：`40677AE9F37A019EF6E737E645E75D39B0D76E1342582810B638B1048B164EF4`

## 5. GitHub 推送状态（已成功）

最初 5 次重试均因旧 token 失效而返回认证错误（原始输出保留在 `evidence/github-push-retry.raw.txt`）。
用户提供新的有效 PAT 后，已把凭据写入本机 Git 凭据管理器（host=`gh-proxy.com`，username=`x-access-token`；token 不写入仓库、不在输出中回显），
并关闭交互提示后重新推送，结果成功：

```
To https://gh-proxy.com/https://github.com/alexwang1982-maker/bb8.git
 * [new branch]      feat/vision-brain-context -> feat/vision-brain-context
branch 'feat/vision-brain-context' set up to track 'origin/feat/vision-brain-context'.
Everything up-to-date
056f32371f386885d68b6b656dee5e5351759152	refs/heads/feat/vision-brain-context
056f32371f386885d68b6b656dee5e5351759152
```

首次成功推送的功能提交 SHA：`056f32371f386885d68b6b656dee5e5351759152`。
当时 `git ls-remote` 确认远端分支与本地 HEAD 完全一致。
随后追加的“推送成功证据”commit 会改变分支 HEAD；最终 SHA 以分支最新提交为准。

原始验证输出：`evidence/github-push-verify.raw.txt`。

## 6. 边界与已知限制

- 视觉观察只在当前轮进入上下文；没有做跨轮持久化（避免扩大范围）。
- 工具拦截是确定性的：视觉轮内除 `ask_brain` / `forward_to_pc` 外的工具调用一律不执行。
  这是安全优先的保守策略；用户明确的物理动作仍由原有快通道（如"左转"）在进入视觉分支之前处理。
- 本分支不包含整份生产 `bridge.py`，只包含可直接应用的真实 diff；原因见第 1 节。
