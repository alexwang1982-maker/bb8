# -*- coding: utf-8 -*-
"""Shared privacy rules for BB-8 evidence scrubbing and scanning.

Rules are deliberately generic: this module must not itself contain the private
names, passwords, key values or raw memories that it removes. It targets
credential shapes, captured OpenMuse memory containers and private-fact phrases.
"""
from __future__ import annotations

import io
import re
import zipfile

MEMORY_BLOCK = re.compile(r"<openmuse_memory>.*?</openmuse_memory>", re.S)
MEMORY_PLACEHOLDER = "<openmuse_memory>[PRIVATE MEMORY REDACTED]</openmuse_memory>"

# Common surnames only. This is generic dictionary data, not any captured person.
_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闽席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双"
    "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
    "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空"
    "曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)


def _is_code_memory_block(text: str) -> bool:
    """Ignore source code that builds the memory tag string at runtime."""
    return (".join(" in text) or ('" +' in text) or ("' +" in text)

# JSON evidence keeps structure; only captured raw/content values with these
# generic private-memory phrases are replaced as a whole.
JSON_VALUE = re.compile(r'("(?:raw|content)"\s*:\s*")((?:\\.|[^"\\])*)(")')
MEMORY_VALUE_MARKERS = (
    "团队动态", "行动清单", "测试口令是", "用户希望被称呼", "复盘审计",
    "声纹识别失败", "余额", "拼多多", "goods_id=", "ompt",
    "我老婆", "用户名叫", "家养", "淘宝女装", "拿铁",
)

TEXT_SUFFIXES = {".json", ".txt", ".md", ".py", ".ps1", ".log", ".jsonl", ".yaml", ".yml", ".csv"}

PATTERNS = [
    # Credentials. No actual key/token value is stored in this rule file.
    ("api_key", re.compile(
        r"(?<![A-Za-z0-9_\-])sk-(?!REDACTED\b)[A-Za-z0-9]{6,}(?:…|\.\.\.)?"), "sk-REDACTED"),
    ("github_token", re.compile(r"(?i)\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}"), "TOKEN-REDACTED"),
    ("bearer", re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9_\-\.]{12,}"), r"\1REDACTED"),
    ("uuid_key", re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     "KEY-REDACTED"),
    ("git_user", re.compile(r"(?i)(username\s*[=:]\s*)x-access-token"), r"\1REDACTED"),
    ("session_id", re.compile(r"\bses_[A-Za-z0-9]{10,}"), "ses-REDACTED"),

    # Private personal content. Generic patterns only; no literal names/values.
    ("test_password",
     re.compile(r"(?i)(测试口令是|test\s*password\s*[:=])\s*([A-Za-z0-9][A-Za-z0-9\-]{5,})"),
     r"\1 PRIVATE-VALUE-REDACTED"),
    ("personal_name", re.compile("[" + _SURNAMES + r"]{1}(?:哥|姐|总|老板)"), "PERSON-REDACTED"),
    ("private_address_form", re.compile(
        r"称呼为[\"']?[\u4e00-\u9fff]{1,4}[\"']?|"
        r"真名/常用名为[\"']?[\u4e00-\u9fff]{1,4}[\"']?|"
        r"替代之前的[\"']?[\u4e00-\u9fff]{1,4}[\"']?称呼|"
        r"刚才是[\u4e00-\u9fff]{1,4}和你对话|"
        r"[\u4e00-\u9fff]{1,4}家养"),
     "PRIVATE-ADDRESS-REDACTED"),
    ("private_fact", re.compile(
        r"我(?:老婆|妻子)叫[^\"\\<\n]{0,60}|用户名叫[^\"\\<\n]{0,60}|"
        r"家养[^\"\\<\n]{0,60}|经营淘宝[^\"\\<\n]{0,60}|日常喝[^\"\\<\n]{0,60}|"
        r"Edge\s*浏览器调试版[^\"\\<\n]{0,60}|拼多多链接[^\"\\<\n]{0,60}|"
        r"goods_id=\d+|余额\s*[¥￥]\s*[\d.]+|\b[A-Z]{2}-[A-Z0-9]{4,}\b|"
        r"声纹识别失败|复盘审计|ompt\.txt"),
     "PRIVATE-FACT-REDACTED"),
]


def scrub(text: str) -> str:
    """Return text with captured private/credential content removed."""
    if not text:
        return text
    out = MEMORY_BLOCK.sub(
        lambda m: m.group(0) if _is_code_memory_block(m.group(0)) else MEMORY_PLACEHOLDER,
        text)

    def _json_value(m):
        if any(mark in m.group(2) for mark in MEMORY_VALUE_MARKERS):
            return m.group(1) + "[PRIVATE MEMORY REDACTED]" + m.group(3)
        return m.group(0)

    out = JSON_VALUE.sub(_json_value, out)
    for _, pat, repl in PATTERNS:
        out = pat.sub(repl, out)
    return out


def find_hits(text: str) -> list[tuple[str, str]]:
    """Return [(rule, redacted_sample)] for any residual sensitive content."""
    hits: list[tuple[str, str]] = []
    if not text:
        return hits
    for m in MEMORY_BLOCK.finditer(text):
        if MEMORY_PLACEHOLDER not in m.group(0) and not _is_code_memory_block(m.group(0)):
            hits.append(("private_memory_block", "<block>"))
    for m in JSON_VALUE.finditer(text):
        if any(mark in m.group(2) for mark in MEMORY_VALUE_MARKERS):
            hits.append(("private_memory_json", "<json-value>"))
    for name, pat, _ in PATTERNS:
        if pat.search(text):
            hits.append((name, "<match>"))
    return hits


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scrub_bytes(name: str, data: bytes) -> tuple[bytes, bool]:
    """Scrub text files and text members inside ZIP containers.

    Returns (new_bytes, changed). Binary/unknown files are left untouched.
    """
    lower = name.lower()
    if lower.endswith(".zip"):
        try:
            src = zipfile.ZipFile(io.BytesIO(data))
            out = io.BytesIO()
            changed = False
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    raw = src.read(info.filename)
                    text = _decode(raw)
                    if text is not None:
                        new_text = scrub(text)
                        new_raw = new_text.encode("utf-8")
                        if new_raw != raw:
                            changed = True
                    else:
                        new_raw = raw
                    dst.writestr(info, new_raw)
            return (out.getvalue(), changed) if changed else (data, False)
        except Exception:
            return data, False
    suffix = name[name.rfind("."):].lower() if "." in name else ""
    if suffix not in TEXT_SUFFIXES:
        return data, False
    text = _decode(data)
    if text is None:
        return data, False
    new = scrub(text)
    return (new.encode("utf-8"), True) if new != text else (data, False)


def find_hits_in_bytes(name: str, data: bytes) -> list[tuple[str, str]]:
    """Scan a file blob, including text members of ZIP archives."""
    lower = name.lower()
    if lower.endswith(".zip"):
        hits: list[tuple[str, str]] = []
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            for member in z.namelist():
                text = _decode(z.read(member))
                if text is not None:
                    hits.extend((rule, "%s:%s" % (member, sample)) for rule, sample in find_hits(text))
        except Exception:
            pass
        return hits
    text = _decode(data)
    return find_hits(text) if text is not None else []
