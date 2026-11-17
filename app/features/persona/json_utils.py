"""从模型回复里抠 JSON：先原样解析，失败再补语法救援截断。

只补括号和引号，不改字段值；补不出来的字段交给调用方按缺省走。
"""

import json
import re


def _close_json(s):
    """把被截断的 JSON 补成合法结构。**只补语法，不改值。**

    逐字符扫：记住未闭合的 `{` / `[`，补引号、补 `null`、去尾逗号。
    字符串里的裸控制字符转义掉，值本身不动。
    """
    out, stack = [], []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ord(ch) < 0x20:
                out.append("\\n" if ch == "\n" else
                           ("\\r" if ch == "\r" else
                            ("\\t" if ch == "\t" else " ")))
            else:
                out.append(ch)
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
            out.append(ch)
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                break
            stack.pop()
            out.append(ch)
        else:
            out.append(ch)
    body = "".join(out).rstrip()
    if in_str:
        body += '"'
    if body.endswith(":"):
        body += " null"
    for closer in reversed(stack):
        body += closer
    body = re.sub(r",\s*([}\]])", r"\1", body)
    return body


def _parse_json_loose(raw):
    """从回复里抠 JSON；截断时补语法再试，不做语义修补。"""
    s = str(raw or "").strip()
    if not s:
        return None
    a = s.find("{")
    if a < 0:
        return None
    b = s.rfind("}")
    if b > a:
        try:
            return json.loads(s[a:b + 1])
        except ValueError:
            pass
    try:
        return json.loads(_close_json(s[a:]))
    except ValueError:
        return None
