# -*- coding: utf-8 -*-
"""把小智转来的任务，转发到 opencode 的 BB-8 语音指令会话。

设计（2026-11-22）：
- 用户要求"直接语音对话执行，不要触发词"，但又要点歌/记忆/音量等老功能不受影响。
- 所以：bridge 里点歌/音量/记忆/摄像头/视觉等快速通道优先照常走；
  剩下走到大模型那一档的对话，用 is_task() 判断"像不像任务指令"，
  像的话 -> forward() 异步转给本会话，小智同时复述确认。
- 回滚：环境变量 OPENMUSE_FORWARD_TO_OPENCODE=0 即可整体关闭。
"""
import subprocess
import glob as _glob
import os

# 2026-11-23：不再写死版本号（2.3.2 升级到 2.4.0 后路径失效 → WinError 2，
# 任务转不出去）。自动找 cli 目录下最新的 opencode-cli.exe。
_CLI_ROOT = os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                         "ai.opencode.desktop", "cli")
def _find_cli():
    cands = _glob.glob(os.path.join(_CLI_ROOT, "*", "opencode-cli.exe"))
    if not cands:
        return ""
    # 按版本号目录名排序取最新（2.10 > 2.9 用数字元组）
    def key(p):
        try:
            return tuple(int(x) for x in os.path.basename(os.path.dirname(p)).split("-")[0].split("."))
        except Exception:
            return (0,)
    return sorted(cands, key=key)[-1]
CLI = _find_cli()

# 2026-11-23：转发目标会话 ID 从配置文件读，不再写死在代码里。
# 新开/更换 BB-8 专用对话时，只改 forward_target.txt 一行即可，不用动代码、不用重启 bridge。
_TARGET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forward_target.txt")
def _read_target():
    try:
        with open(_TARGET_FILE, encoding="utf-8") as f:
            sid = f.read().strip()
        if sid.startswith("ses_"):
            return sid
    except Exception:
        pass
    return "ses_df750d6beffekx9QOwDS0VyJ3i"  # 默认：BB8语音指令通道专用会话
SID = _read_target()

# 2026-11-22 二次修正：用户明确要"模式切换"语义——
#   正常聊天就正常聊（哪怕提到"电脑助手模式"也不转）；
#   只有明确的"启动/切换助手模式"或"问下/问一下opencode+任务"才转。
# 判据：必须有明确的"转达动作"开头，光提到名字不算。
CALL_VERBS = (
    "启动助手", "启动电脑助手", "启动编码助手", "开启助手", "开启电脑助手",
    "启动电脑模式", "电脑助手模式", "助手模式", "启动电脑",
    "切换助手", "切换到助手", "切换电脑助手", "切换成助手", "切换成电脑助手",
    "进入助手", "进入电脑助手", "打开助手模式", "开启助手模式",
    "问下opencode", "问一下opencode", "问下open", "问一下open",
    "问下电脑助手", "问一下电脑助手", "问问opencode", "问问电脑助手",
    "问一下他下一步", "问一下它下一步", "问下他下一步", "问下它下一步",
    "问一下他接下来", "问一下它接下来", "问一下他要干嘛", "问一下它要干嘛",
    "连上opencode", "连接opencode", "连接上opencode", "连接电脑助手",
    "让opencode", "让电脑助手", "交给opencode", "交给电脑助手",
    "转给opencode", "转给电脑助手", "发给opencode", "发给电脑助手",
    "传给opencode", "传给电脑助手", "用opencode", "用电脑助手",
    "连接我电脑上的", "连接我电脑的", "连到我电脑上的", "连到我电脑的",
    "连接上我电脑", "连接我的电脑", "连上我的电脑",
)
# 即便含动作词，若整句是明显"问句/聊天"（以这些结尾或含这些且无任务动词）也不转
CHAT_ONLY_END = ("吗", "呢", "吧", "嘛", "呀", "吗？", "么", "?")


def is_task(text):
    """只有明确的"启动/切换助手模式"或"问下opencode+任务"才转 -> True。
    纯聊天（哪怕提到"电脑助手模式"）不转。"""
    t = (text or "").strip().lower().replace(" ", "")
    if len(t) < 2:
        return False
    hit = False
    for w in CALL_VERBS:
        if w.lower().replace(" ", "") in t:
            hit = True
            break
    if not hit:
        return False
    # 排除"纯聊天/元讨论"：用户在讨论规则本身（注意/记清楚/你为什么/是不是/不是我说的），不是下任务。
    META_CHAT = ("注意", "记清楚", "记住", "你为什么", "为什么不", "是不是", "你不是",
                 "我跟你", "我只是在", "聊天", "没让你", "不要犯", "懂没", "听到没",
                 "你看看你", "傻", "妈的", "牛逼", "别傻")
    if any(m in t for m in META_CHAT):
        return False
    # 以"吗/呢/嘛/呀"结尾且没有任何明确任务动词（查/改/建/看/问下X做）-> 纯问句，不转
    if t.endswith(CHAT_ONLY_END):
        TASK_HINT = ("查", "改", "建", "创建", "删", "看", "发", "下一步", "接下来", "要干嘛", "怎么做", "干嘛")
        if not any(h in t for h in TASK_HINT):
            return False
    return True


def clean_task(text):
    """去掉触发前缀和废话，只留实际任务（用户：不要原封不动传过去，要总结需求）。"""
    import re
    t = (text or "").strip()
    # 去掉开头的前缀短语（可连续多个），支持 ASR 把 opencode 识别成各种错拼
    prefix_re = re.compile(
        r"^(?:"
        r"(?:启动|开启|打开|进入|切换(?:到|成)?)?(?:助手|电脑助手|编码助手)(?:模式)?"
        r"|问(?:一下|下|问)?(?:opencode|电脑助手|编码助手|[o0]\s*[pb]\s*[ea]\s*\w+)"
        r"|(?:连接|连上|连到)(?:上)?(?:我|我的)?(?:电脑(?:上|的|上的)?)?(?:opencode|电脑助手|编码助手|[o0]\s*[pb]\s*\w+)"
        r"|(?:让|交给|转给|发给|传给|用)(?:opencode|电脑助手|编码助手)"
        r"|,|，|。|、|：|:|吗|呢|吧|啊|呀"
        r")+", re.I)
    prev = None
    while prev != t:
        prev = t
        t = prefix_re.sub("", t).strip()
    return t or text.strip()


def forward(text, session_id=None, timeout=8):
    """把 text 发到 opencode 会话。返回 (ok, 简讯)。
    session_id=None 时每次现读 forward_target.txt（改文件即生效，无需重启）。"""
    import json
    if session_id is None:
        session_id = _read_target()
    body = json.dumps({"text": text}, ensure_ascii=False)
    cmd = [CLI, "api", "session.prompt", "--param", "sessionID=" + session_id, "-d", body]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (r.stdout or "").strip()
        ok = (r.returncode == 0) and ('"_tag"' not in out)
        return ok, (out[:120] if ok else ("rc=%s %s" % (r.returncode, out[:120])))
    except Exception as e:
        return False, str(e)[:120]


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    for s in sys.argv[1:]:
        print(repr(s), "task=", is_task(s))
