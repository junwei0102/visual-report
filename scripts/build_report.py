#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_report.py —— 把一份 Markdown 汇报稿编译成自带样式、不依赖网络的单文件 HTML。

    python3 build_report.py 报告.md [--out 文件 | --out-dir 目录] [--title 标题]
                            [--kind plan|result|explain|note] [--project 名字] [--open] [--quiet]

最后一行输出生成的 HTML 的绝对路径（--quiet 时只输出这一行）。

除了常见 Markdown（标题、段落、列表、表格、代码块、引用、图片、链接），还认识下面这些「组件」。
组件都写成围栏代码块：```名字 说明文字 … ```，里面的内容仍然是 Markdown。

    tldr                 开头的一句话结论框（说明文字 = 左上角的小标签）
    callout 类型 标题     提示框；类型 tip / warn / good / bad / note
    steps                带编号和状态的步骤：列表项开头写 [x] 已完成 / [~] 进行中 / [ ] 待做 / [!] 卡住 / [-] 跳过
    compare / cards      用一行 --- 分成几栏并排（窄屏自动变成上下排）
    details 标题         默认折叠的细节
    kv                   「键: 值」列表
    flow 图注            流程 / 架构图：A -> B -> C；A -> D : 箭头上的字；..> 虚线；=> 强调；
                         ((文字)) 圆角起止框；{文字} 判断框；文字! 高亮；一行 direction: TB 改成竖排
    bar 图注             横向条形图：名字: 数值（行尾加 ! 高亮；可写 max: 100、unit: %）
    svg / html           原样输出（手写 SVG 用）

行内：[[术语|白话解释]] 悬停显示解释；==高亮==；本地图片 ![说明](路径) 会直接嵌进 HTML。
只依赖 Python ≥ 3.8 标准库。
"""
import argparse
import base64
import datetime as _dt
import html as _html
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_DIR / "assets" / "template.html"
DEFAULT_BASE_DIR = Path(os.environ.get("VISUAL_REPORT_DIR", Path.home() / ".claude" / "visual-reports"))

KIND_LABELS = {"plan": "计划", "result": "结果", "explain": "解释", "note": "说明"}
TLDR_LABELS = {"plan": "打算这么做", "result": "结论", "explain": "一句话", "note": "要点"}
CALLOUT_TYPES = {"tip": "提示", "warn": "注意", "good": "没问题", "bad": "有问题", "note": "说明"}
TASK_STATUS = {"x": "done", "X": "done", "~": "doing", " ": "todo", "!": "blocked", "-": "skipped"}
MARKDOWN_FENCES = {"tldr", "summary", "callout", "steps", "compare", "cards", "details", "kv"}


def esc(s):
    return _html.escape(s, quote=True)


def warn(msg):
    sys.stderr.write("警告：%s\n" % msg)


# --------------------------------------------------------------------------- 行内
CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
CJK = "\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f"
CJK_JOIN_RE = re.compile("(?<=[%s])\n(?=[%s])" % (CJK, CJK))


def render_inline(text, ctx):
    codes = []

    def stash(m):
        codes.append("<code>%s</code>" % esc(m.group(2).strip()))
        return "\x00%d\x00" % (len(codes) - 1)

    text = CODE_SPAN_RE.sub(stash, text)
    text = re.sub(r"(?:  |\\)\n", "\x01", text)  # 行尾两个空格或反斜杠 = 强制换行
    text = CJK_JOIN_RE.sub("", text)                 # 中文字符之间的换行直接去掉，否则浏览器会显示一个空格
    text = text.replace("\n", " ")
    text = esc(text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", lambda m: img_tag(m.group(1), m.group(2), ctx), text)
    text = re.sub(r"\[\[([^\]|]+?)\|([^\]]+?)\]\]", r'<span class="term" tabindex="0" data-tip="\2">\1</span>', text)
    text = re.sub(r"\[\[([^\]|]+?)\]\]", r'<span class="term">\1</span>', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    text = re.sub(r"==(.+?)==", r"<mark>\1</mark>", text)
    text = text.replace("\x01", "<br>")
    text = re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], text)
    return text


def resolve_image(src, ctx):
    """本地图片 → data URI，保证 HTML 单文件可用。"""
    if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", src, re.I):
        return src
    src_unescaped = _html.unescape(src)
    candidates = [ctx["base_dir"] / src_unescaped, Path.cwd() / src_unescaped, Path(src_unescaped).expanduser()]
    for p in candidates:
        if p.is_file():
            if p.stat().st_size > 8 * 1024 * 1024:
                warn("图片 %s 超过 8MB，没有嵌入，HTML 里保留的是路径" % p)
                return src
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            if p.suffix.lower() == ".svg":
                mime = "image/svg+xml"
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            return "data:%s;base64,%s" % (mime, data)
    warn("找不到图片 %s（相对 %s 或当前目录）" % (src_unescaped, ctx["base_dir"]))
    return src


def img_tag(alt, src, ctx):
    return '<img src="%s" alt="%s">' % (resolve_image(src, ctx), alt)


def render_figure_image(alt, src, ctx):
    cap = '<figcaption>%s</figcaption>' % render_inline(alt, ctx) if alt.strip() else ""
    return '<figure class="image">%s%s</figure>' % (img_tag(esc(alt), esc(src), ctx), cap)


# --------------------------------------------------------------------------- 块级
FENCE_RE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})\s*([^\s`]*)\s*(.*)$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
HTML_START_RE = re.compile(r"^\s{0,3}<(?:/?([a-zA-Z][\w-]*)|(!--))")
QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
IMG_ONLY_RE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
TASK_RE = re.compile(r"^\[( |x|X|~|!|-)\]\s+(.*)$")
VOID_TAGS = {"img", "br", "hr", "input", "meta", "link", "source", "track", "wbr", "embed"}


def is_block_start(line):
    return bool(FENCE_RE.match(line) or HEADING_RE.match(line) or HR_RE.match(line)
                or LIST_RE.match(line) or QUOTE_RE.match(line) or HTML_START_RE.match(line))


def dedent(line, k):
    i = 0
    while i < k and i < len(line) and line[i] == " ":
        i += 1
    return line[i:]


def parse_blocks(lines, ctx):
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = FENCE_RE.match(line)
        if m:
            indent, fence, lang, info = len(m.group(1)), m.group(2), m.group(3).lower(), m.group(4).strip()
            nest = lang in MARKDOWN_FENCES  # 内容是 Markdown 的组件：里面还可以再套带语言名的代码块
            j, body, inner = i + 1, [], []   # inner：当前打开着的内层围栏的长度栈
            while j < n:
                l = lines[j]
                fm = FENCE_RE.match(l)
                if fm and fm.group(2)[0] == fence[0]:
                    length, tagged = len(fm.group(2)), bool(fm.group(3))
                    if inner:
                        if not tagged and length >= inner[-1]:
                            inner.pop()          # 内层代码块结束
                        elif tagged and nest:
                            inner.append(length)
                    else:
                        if not tagged and length >= len(fence):
                            break                # 外层结束（和标准 Markdown 一样：同样长度的裸围栏就是结束）
                        if nest and tagged:
                            inner.append(length)  # 内层带语言名的代码块开始
                body.append(dedent(l, indent))
                j += 1
            if j >= n:
                warn("第 %d 行的 %s 没有配对的结束符，之后的内容全被当成了这个块" % (ctx["line_base"] + i + 1, fence))
            elif nest and looks_like_nesting_mistake(lines, j, fence):
                warn("第 %d 行的 %s 结束了 %s 块，但紧接着又出现了一个不带语言名的 %s。如果你是想在 %s 里面放代码块，"
                     "请给内层代码块写上语言名（如 ```text），或者把外层改成四个反引号"
                     % (ctx["line_base"] + j + 1, fence, lang, fence, lang))
            sub = dict(ctx)
            sub["line_base"] = ctx["line_base"] + i + 1
            out.append(render_fence(lang, info, body, sub))
            i = j + 1
            continue
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, render_inline(m.group(2), ctx), level))
            i += 1
            continue
        if HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue
        m = HTML_START_RE.match(line)
        if m:
            j, block = collect_html_block(lines, i, m.group(1))
            check_svg(block, ctx["line_base"] + i + 1)
            out.append("\n".join(block))
            i = j
            continue
        if "|" in line and i + 1 < n and "|" in lines[i + 1] and TABLE_SEP_RE.match(lines[i + 1]):
            j = i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                j += 1
            out.append(render_table(lines[i], lines[i + 1], lines[i + 2:j], ctx))
            i = j
            continue
        if QUOTE_RE.match(line):
            j, inner = i, []
            while j < n and lines[j].strip():
                qm = QUOTE_RE.match(lines[j])
                inner.append(qm.group(1) if qm else lines[j])
                j += 1
            out.append("<blockquote>%s</blockquote>" % parse_blocks(inner, ctx))
            i = j
            continue
        if LIST_RE.match(line):
            j = collect_list_block(lines, i)
            out.append(render_list(lines[i:j], ctx))
            i = j
            continue
        m = IMG_ONLY_RE.match(line)
        if m:
            out.append(render_figure_image(m.group(1), m.group(2), ctx))
            i += 1
            continue
        j, para = i, []
        while j < n and lines[j].strip() and (j == i or not is_block_start(lines[j])):
            if j > i and "|" in lines[j] and j + 1 < n and TABLE_SEP_RE.match(lines[j + 1]):
                break
            para.append(lines[j].strip())
            j += 1
        out.append("<p>%s</p>" % render_inline("\n".join(para), ctx))
        i = j
    return "\n".join(out)


def looks_like_nesting_mistake(lines, j, fence):
    """组件块在第 j 行被一个裸围栏结束了。如果后面紧跟着几行普通文字、然后又是一个裸围栏，
    多半是作者想在组件里放一个没写语言名的代码块，结果被提前截断了。"""
    for k in range(j + 1, min(len(lines), j + 15)):
        l = lines[k]
        if not l.strip():
            continue
        fm = FENCE_RE.match(l)
        if fm:
            return fm.group(2)[0] == fence[0] and len(fm.group(2)) >= len(fence) and not fm.group(3)
        if is_block_start(l):
            return False
    return False


def collect_html_block(lines, i, tag):
    n, j, block = len(lines), i, []
    if tag is None:  # <!-- 注释 -->
        while j < n:
            block.append(lines[j])
            j += 1
            if "-->" in lines[j - 1]:
                break
        return j, block
    open_re = re.compile(r"<" + re.escape(tag) + r"(?=[\s>/])", re.I)
    self_re = re.compile(r"<" + re.escape(tag) + r"\b[^>]*/>", re.I)
    close_re = re.compile(r"</" + re.escape(tag) + r"\s*>", re.I)
    depth = 0
    while j < n:
        l = lines[j]
        block.append(l)
        j += 1
        if tag.lower() in VOID_TAGS:
            if j >= n or not lines[j].strip():
                break
            continue
        depth += len(open_re.findall(l)) - len(self_re.findall(l)) - len(close_re.findall(l))
        if depth <= 0:
            break
    return j, block


def check_svg(block, lineno):
    text = "\n".join(block).strip()
    if not text.lower().startswith("<svg"):
        return
    try:
        ET.fromstring(text)
    except ET.ParseError as e:
        warn("第 %d 行开始的 <svg> 不是合法 XML（%s），浏览器可能画不全。常见原因：标签没闭合、属性值没加引号、用了 &nbsp; 这类实体" % (lineno, e))


def marker_kind(marker):
    return "ol" if marker[0].isdigit() else "ul"


def collect_list_block(lines, i):
    n = len(lines)
    first = LIST_RE.match(lines[i])
    base, kind = len(first.group(1)), marker_kind(first.group(2))

    def same_list_item(lm):
        return lm and (len(lm.group(1)) > base or (len(lm.group(1)) == base and marker_kind(lm.group(2)) == kind))

    j = i + 1
    while j < n:
        l = lines[j]
        if not l.strip():
            k = j
            while k < n and not lines[k].strip():
                k += 1
            if k >= n:
                break
            nxt = lines[k]
            ind = len(nxt) - len(nxt.lstrip())
            if same_list_item(LIST_RE.match(nxt)) or (ind > base and not LIST_RE.match(nxt)):
                j = k
                continue
            break
        lm = LIST_RE.match(l)
        ind = len(l) - len(l.lstrip())
        if same_list_item(lm) or (ind > base and not lm) or (not lm and not is_block_start(l)):
            j += 1
            continue
        break
    return j


def split_list_items(block):
    base = len(LIST_RE.match(block[0]).group(1))
    items = []
    content_indent = base + 2
    for l in block:
        m = LIST_RE.match(l)
        if m and len(m.group(1)) == base:
            content_indent = base + len(m.group(2)) + 1
            items.append({"marker": m.group(2), "first": [m.group(3)], "rest": []})
            continue
        cur = items[-1]
        stripped = dedent(l, content_indent) if l.strip() else ""
        if not cur["rest"] and stripped.strip() and not is_block_start(stripped):
            cur["first"].append(stripped.strip())
        else:
            cur["rest"].append(stripped)
    return items


def render_list(block, ctx, steps=False):
    items = split_list_items(block)
    ordered = items[0]["marker"][0].isdigit()
    tag = "ol" if (ordered or steps) else "ul"
    cls = ' class="steps"' if steps else ""
    out = ["<%s%s>" % (tag, cls)]
    for it in items:
        first = "\n".join(it["first"])
        status = ""
        tm = TASK_RE.match(first)
        if tm:
            status = TASK_STATUS[tm.group(1)]
            first = tm.group(2)
        rest_html = parse_blocks(it["rest"], ctx) if any(x.strip() for x in it["rest"]) else ""
        if steps:
            bm = re.match(r"^\*\*(.+?)\*\*[\s:：—-]*(.*)$", first, re.S)
            if bm:
                title, body = bm.group(1), bm.group(2)
            else:
                title, body = first, ""
            body_html = ("<p>%s</p>" % render_inline(body, ctx) if body.strip() else "") + rest_html
            out.append('<li class="%s"><div class="step-num"></div><div class="step-main"><div class="step-title">%s</div>%s</div></li>'
                       % (status, render_inline(title, ctx), '<div class="step-body">%s</div>' % body_html if body_html else ""))
        else:
            li_cls = ' class="task %s"' % status if status else ""
            inner = render_inline(first, ctx)
            if rest_html:
                inner = "<p>%s</p>%s" % (inner, rest_html)
            out.append("<li%s>%s</li>" % (li_cls, inner))
    out.append("</%s>" % tag)
    return "\n".join(out)


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.replace("\\|", "|").strip() for c in cells]


def render_table(head, sep, rows, ctx):
    heads = split_row(head)
    aligns = []
    for c in split_row(sep):
        c = c.strip()
        if c.startswith(":") and c.endswith(":"):
            aligns.append("center")
        elif c.endswith(":"):
            aligns.append("num")
        else:
            aligns.append("")
    while len(aligns) < len(heads):
        aligns.append("")

    def cell(tag, i, text):
        a = aligns[i] if i < len(aligns) else ""
        return "<%s%s>%s</%s>" % (tag, ' class="%s"' % a if a else "", render_inline(text, ctx), tag)

    out = ['<div class="table-wrap"><table><thead><tr>']
    out += [cell("th", i, h) for i, h in enumerate(heads)]
    out.append("</tr></thead><tbody>")
    for r in rows:
        cells = split_row(r)
        cells += [""] * (len(heads) - len(cells))
        out.append("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(cells[:len(heads)])) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


# --------------------------------------------------------------------------- 组件
def split_columns(body):
    cols, cur = [], []
    for l in body:
        if HR_RE.match(l):
            cols.append(cur)
            cur = []
        else:
            cur.append(l)
    cols.append(cur)
    return [c for c in cols if any(x.strip() for x in c)]


def render_fence(lang, info, body, ctx):
    sub = ctx
    if lang in ("tldr", "summary"):
        label = info or TLDR_LABELS.get(ctx["kind"], "一句话")
        return '<section class="tldr"><div class="eyebrow">%s</div>%s</section>' % (esc(label), parse_blocks(body, sub))
    if lang == "callout":
        parts = info.split(None, 1)
        typ = parts[0].lower() if parts and parts[0].lower() in CALLOUT_TYPES else "note"
        if parts and parts[0].lower() in CALLOUT_TYPES:
            title = parts[1] if len(parts) > 1 else CALLOUT_TYPES[typ]
        else:
            title = info or CALLOUT_TYPES[typ]
        return '<aside class="callout %s"><div class="callout-title">%s</div>%s</aside>' % (typ, render_inline(title, ctx), parse_blocks(body, sub))
    if lang == "steps":
        first_list = next((k for k, l in enumerate(body) if LIST_RE.match(l)), None)
        if first_list is None:
            return parse_blocks(body, sub)
        head = parse_blocks(body[:first_list], sub)
        j = collect_list_block(body, first_list)
        tail = parse_blocks(body[j:], sub)
        return head + render_list(body[first_list:j], sub, steps=True) + tail
    if lang in ("compare", "cards"):
        cols = split_columns(body)
        return '<div class="%s">%s</div>' % (lang, "".join('<div class="col">%s</div>' % parse_blocks(c, sub) for c in cols))
    if lang == "details":
        return '<details><summary>%s</summary><div class="details-body">%s</div></details>' % (render_inline(info or "展开细节", ctx), parse_blocks(body, sub))
    if lang == "kv":
        rows = []
        for l in body:
            if not l.strip():
                continue
            m = re.match(r"^\s*(.+?)\s*[:：]\s+(.*)$", l) or re.match(r"^\s*(.+?)\s*[:：](.*)$", l)
            if m:
                rows.append("<dt>%s</dt><dd>%s</dd>" % (render_inline(m.group(1), ctx), render_inline(m.group(2).strip(), ctx)))
            else:
                rows.append("<dt></dt><dd>%s</dd>" % render_inline(l.strip(), ctx))
        return '<dl class="kv">%s</dl>' % "".join(rows)
    if lang == "flow":
        return render_flow(info, body, ctx)
    if lang == "bar":
        return render_bar(info, body, ctx)
    if lang in ("svg", "html", "raw"):
        text = "\n".join(body)
        check_svg(body, ctx["line_base"] + 1)
        return text
    if lang == "mermaid":
        warn("报告里有 mermaid 代码块，这里没有渲染器，会显示成源码。请改用 flow 组件或手写 SVG")
    code = '<pre><code%s>%s</code></pre>' % (' class="language-%s"' % esc(lang) if lang else "", esc("\n".join(body)))
    if info:
        return '<div class="code-wrap"><div class="code-head">%s</div>%s</div>' % (esc(info), code)
    return code


# --------------------------------------------------------------------------- 流程图
FONT = 13
LINE_H = 17


def text_w(s):
    return sum(FONT if ord(c) > 0x2E7F else FONT * 0.56 for c in s)


NO_LINE_START = set("，。？！、：；）」』》”’,.?!:;)")


def wrap_label(label, max_w=FONT * 9):
    """没手动换行、又太长的标签，折成 2–3 行：有空格先按空格折，否则按宽度均分。"""
    if "\n" in label or text_w(label) <= max_w:
        return label.split("\n")
    words = label.split(" ")
    if len(words) > 1:
        lines, cur = [], ""
        for w in words:
            cand = (cur + " " + w).strip()
            if cur and text_w(cand) > max_w:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
        if len(lines) <= 3 and all(text_w(l) <= max_w * 1.2 for l in lines):
            return lines
    n = min(3, int(-(-text_w(label) // max_w)))
    target = text_w(label) / n
    lines, cur, acc = [], "", 0.0
    for idx, ch in enumerate(label):
        cur += ch
        acc += FONT if ord(ch) > 0x2E7F else FONT * 0.56
        nxt = label[idx + 1] if idx + 1 < len(label) else ""
        if acc >= target and len(lines) < n - 1 and nxt and nxt not in NO_LINE_START:
            lines.append(cur)
            cur, acc = "", 0.0
    if cur:
        lines.append(cur)
    return lines


ARROW_RE = re.compile(r"\s*(-+>|\.{2,}>|=+>)\s*")
LABEL_SPLIT_RE = re.compile(r"^(.*?)(?:\s+:\s*|:\s+)(.*)$")


def parse_flow(body):
    direction = "LR"
    nodes, order, edges = {}, [], []

    def get_node(tok):
        tok = tok.strip()
        hl = False
        if tok.endswith("!") and len(tok) > 1:
            hl, tok = True, tok[:-1].rstrip()
        shape = "box"
        if tok.startswith("((") and tok.endswith("))"):
            shape, tok = "pill", tok[2:-2]
        elif tok.startswith("{") and tok.endswith("}"):
            shape, tok = "decision", tok[1:-1]
        elif tok.startswith("[") and tok.endswith("]"):
            tok = tok[1:-1]
        elif tok.startswith("(") and tok.endswith(")"):
            shape, tok = "pill", tok[1:-1]
        label = tok.strip().replace("\\n", "\n")
        key = label.replace("\n", " ")
        if key not in nodes:
            nodes[key] = {"label": label, "shape": shape, "hl": hl}
            order.append(key)
        else:
            nodes[key]["hl"] = nodes[key]["hl"] or hl
            if shape != "box":
                nodes[key]["shape"] = shape
        return key

    for raw in body:
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        m = re.match(r"^(?:direction|dir)\s*[:=]?\s*(LR|TB|TD)\s*$", s, re.I)
        if m:
            direction = "TB" if m.group(1).upper() in ("TB", "TD") else "LR"
            continue
        parts = ARROW_RE.split(s)
        if len(parts) == 1:
            get_node(parts[0])
            continue
        prev = None
        for idx in range(0, len(parts), 2):
            seg, label = parts[idx], ""
            if idx > 0:
                lm = LABEL_SPLIT_RE.match(seg)
                if lm:
                    seg, label = lm.group(1), lm.group(2)
            if not seg.strip():
                continue
            node = get_node(seg)
            if prev is not None:
                arrow = parts[idx - 1].strip()
                style = "dashed" if arrow.startswith(".") else "strong" if arrow.startswith("=") else "solid"
                edges.append({"src": prev, "dst": node, "label": label.strip(), "style": style})
            prev = node
    return direction, nodes, order, edges


def layer_graph(order, edges):
    succ = {k: [] for k in order}
    for e in edges:
        succ[e["src"]].append(e["dst"])
    state, back = {}, set()

    def dfs(u):
        state[u] = 1
        for v in succ[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    for k in order:
        if k not in state:
            dfs(k)
    for e in edges:
        e["back"] = (e["src"], e["dst"]) in back
    indeg = {k: 0 for k in order}
    fwd = {k: [] for k in order}
    for e in edges:
        if not e["back"]:
            fwd[e["src"]].append(e["dst"])
            indeg[e["dst"]] += 1
    layer = {k: 0 for k in order}
    queue = [k for k in order if indeg[k] == 0]
    while queue:
        u = queue.pop(0)
        for v in fwd[u]:
            layer[v] = max(layer[v], layer[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    nlayers = max(layer.values()) + 1 if layer else 0
    cols = [[k for k in order if layer[k] == l] for l in range(nlayers)]
    rank = {}
    for l, col in enumerate(cols):
        if l > 0:
            def bary(k):
                preds = [e["src"] for e in edges if e["dst"] == k and not e["back"]]
                return sum(rank[p] for p in preds) / len(preds) if preds else 0.5
            col.sort(key=bary)
        for i, k in enumerate(col):
            rank[k] = (i + 0.5) / len(col)
    return cols


_diagram_counter = [0]


def svg_text_lines(cx, cy, lines, cls=""):
    y0 = cy - (len(lines) - 1) * LINE_H / 2
    spans = "".join('<tspan x="%.1f" dy="%s">%s</tspan>' % (cx, "0" if i == 0 else LINE_H, esc(l)) for i, l in enumerate(lines))
    return '<text x="%.1f" y="%.1f" text-anchor="middle" dominant-baseline="central"%s>%s</text>' % (cx, y0, ' class="%s"' % cls if cls else "", spans)


def render_flow(info, body, ctx):
    direction, nodes, order, edges = parse_flow(body)
    if not order:
        return '<p class="callout bad">flow 组件里没有读到任何节点</p>'
    cols = layer_graph(order, edges)
    PAD_X, MIN_W, BASE_H, MARGIN = 13, 60, 38, 14
    GAP_MAIN = 56 if direction == "LR" else 50   # 层与层之间（给箭头上的字留地方）
    GAP_CROSS = 22 if direction == "LR" else 26  # 同一层内框与框之间
    layer_of = {k: l for l, col in enumerate(cols) for k in col}
    for k, nd in nodes.items():
        lines = wrap_label(nd["label"])
        tw = max(text_w(l) for l in lines)
        h = BASE_H + LINE_H * (len(lines) - 1)
        if nd["shape"] == "pill":
            w = max(MIN_W, tw + 2 * PAD_X + 10)
        elif nd["shape"] == "decision":
            w, h = tw + 2 * PAD_X + 30, h + 14
        else:
            w = max(MIN_W, tw + 2 * PAD_X)
        nd.update(lines=lines, w=w, h=h)
    has_back = any(e["back"] for e in edges)
    top_pad = 30 if has_back else 0
    # 层与层之间的距离：至少 GAP_MAIN，横排时还要放得下这一段箭头上最宽的文字
    gaps = []
    for l in range(len(cols) - 1):
        labels_here = [e["label"] for e in edges if not e["back"] and e["label"]
                       and layer_of[e["src"]] == l and layer_of[e["dst"]] == l + 1]
        widest = max((text_w(t) * 11.5 / FONT for t in labels_here), default=0)
        gaps.append(max(GAP_MAIN, widest + 24) if direction == "LR" else GAP_MAIN)
    gaps.append(0)
    if direction == "LR":
        col_w = [max(nodes[k]["w"] for k in col) for col in cols]
        stacks = [sum(nodes[k]["h"] for k in col) + GAP_CROSS * (len(col) - 1) for col in cols]
        max_stack = max(stacks)
        x = MARGIN
        for l, col in enumerate(cols):
            y = MARGIN + top_pad + (max_stack - stacks[l]) / 2
            for k in col:
                nd = nodes[k]
                nd["x"], nd["y"] = x + (col_w[l] - nd["w"]) / 2, y
                y += nd["h"] + GAP_CROSS
            x += col_w[l] + gaps[l]
        W, H = x + MARGIN, max_stack + 2 * MARGIN + top_pad
    else:
        row_h = [max(nodes[k]["h"] for k in col) for col in cols]
        spans = [sum(nodes[k]["w"] for k in col) + GAP_CROSS * (len(col) - 1) for col in cols]
        max_span = max(spans)
        y = MARGIN
        for l, col in enumerate(cols):
            x = MARGIN + (max_span - spans[l]) / 2
            for k in col:
                nd = nodes[k]
                nd["x"], nd["y"] = x, y + (row_h[l] - nd["h"]) / 2
                x += nd["w"] + GAP_CROSS
            y += row_h[l] + gaps[l]
        W, H = max_span + 2 * MARGIN + (34 if has_back else 0), y + MARGIN
    if direction == "LR" and W > 820:
        warn("流程图很宽（%.0f px），会被缩小。可以加一行 direction: TB 改成竖排，或者用 \\n 给长标签手动换行" % W)
    for nd in nodes.values():
        nd["cx"], nd["cy"] = nd["x"] + nd["w"] / 2, nd["y"] + nd["h"] / 2

    _diagram_counter[0] += 1
    did = "d%d" % _diagram_counter[0]
    out = ['<defs>'
           '<marker id="%s-a" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arrowhead"/></marker>'
           '<marker id="%s-s" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arrowhead strong"/></marker>'
           '</defs>' % (did, did)]
    labels = []
    for e in edges:
        s, d = nodes[e["src"]], nodes[e["dst"]]
        marker = "%s-s" % did if e["style"] == "strong" else "%s-a" % did
        if direction == "LR":
            if e["back"]:
                x1, y1, x2, y2 = s["cx"], s["y"], d["cx"], d["y"]
                yt = MARGIN - 2
                path = "M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (x1, y1, x1, yt, x2, yt, x2, y2)
                lx, ly = (x1 + x2) / 2, (y1 + y2 + 6 * yt) / 8 - 4
            else:
                x1, y1, x2, y2 = s["x"] + s["w"], s["cy"], d["x"], d["cy"]
                c = (x2 - x1) * 0.5
                path = "M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (x1, y1, x1 + c, y1, x2 - c, y2, x2, y2)
                lx, ly = (x1 + x2) / 2, (y1 + y2) / 2 - 7
        else:
            if e["back"]:
                x1, y1, x2, y2 = s["x"] + s["w"], s["cy"], d["x"] + d["w"], d["cy"]
                xr = W - MARGIN + 2
                path = "M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (x1, y1, xr, y1, xr, y2, x2, y2)
                lx, ly = (x1 + x2 + 6 * xr) / 8 - 6, (y1 + y2) / 2 - 7
            else:
                x1, y1, x2, y2 = s["cx"], s["y"] + s["h"], d["cx"], d["y"]
                c = (y2 - y1) * 0.5
                path = "M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (x1, y1, x1, y1 + c, x2, y2 - c, x2, y2)
                lx, ly = (x1 + x2) / 2, (y1 + y2) / 2 - 7
        out.append('<path class="edge %s" d="%s" marker-end="url(#%s)"/>' % (e["style"], path, marker))
        if e["label"]:
            labels.append('<text class="edge-label" x="%.1f" y="%.1f" text-anchor="middle">%s</text>' % (lx, ly, esc(e["label"])))
    for k in order:
        nd = nodes[k]
        cls = "node %s%s" % (nd["shape"], " hl" if nd["hl"] else "")
        if nd["shape"] == "decision":
            pts = "%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" % (nd["x"], nd["cy"], nd["cx"], nd["y"], nd["x"] + nd["w"], nd["cy"], nd["cx"], nd["y"] + nd["h"])
            shape = '<polygon points="%s"/>' % pts
        else:
            rx = nd["h"] / 2 if nd["shape"] == "pill" else 9
            shape = '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f"/>' % (nd["x"], nd["y"], nd["w"], nd["h"], rx)
        out.append('<g class="%s">%s%s</g>' % (cls, shape, svg_text_lines(nd["cx"], nd["cy"], nd["lines"])))
    out += labels
    aria = esc(info) if info else "流程图"
    svg = ('<svg class="flow" viewBox="0 0 %.0f %.0f" width="%.0f" height="%.0f" style="width:min(100%%,%.0fpx);min-width:%.0fpx" role="img" aria-label="%s">%s</svg>'
           % (W, H, W, H, W, min(W, max(W * 0.72, 300)), aria, "".join(out)))
    cap = "<figcaption>%s</figcaption>" % render_inline(info, ctx) if info else ""
    return '<figure class="diagram">%s%s</figure>' % (svg, cap)


# --------------------------------------------------------------------------- 条形图
def fmt_num(v):
    if abs(v - round(v)) < 1e-9:
        return "{:,}".format(int(round(v)))
    return ("%.2f" % v).rstrip("0").rstrip(".")


def render_bar(info, body, ctx):
    rows, vmax, unit = [], None, ""
    for raw in body:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(max|unit)\s*[:=]\s*(.+)$", s, re.I)
        if m:
            if m.group(1).lower() == "max":
                try:
                    vmax = float(m.group(2).replace(",", ""))
                except ValueError:
                    warn("bar 组件的 max 不是数字：%s" % m.group(2))
            else:
                unit = m.group(2).strip()
            continue
        m = re.match(r"^(.+?)\s*[:：]\s*(-?[\d.,]+)\s*([^\s!]*)\s*(!?)$", s)
        if not m:
            warn("bar 组件里这一行看不懂，已跳过：%s" % s)
            continue
        rows.append((m.group(1), float(m.group(2).replace(",", "")), m.group(3), bool(m.group(4))))
    if not rows:
        return '<p class="callout bad">bar 组件里没有读到数据</p>'
    data_max = max(v for _, v, _, _ in rows)
    scale_max = vmax if vmax else (data_max if data_max > 0 else 1)
    label_w = max(text_w(l) for l, _, _, _ in rows) + 10
    value_w = max(text_w(fmt_num(v) + (u or unit)) for _, v, u, _ in rows) + 14
    MARGIN, ROW_H, BAR_H = 10, 30, 16
    W = 560
    bar_x = MARGIN + label_w
    bar_max_w = max(120, W - bar_x - value_w - MARGIN)
    H = len(rows) * ROW_H + 2 * MARGIN
    out = []
    for i, (label, v, u, hl) in enumerate(rows):
        y = MARGIN + i * ROW_H
        fill_w = max(0.0, min(1.0, v / scale_max)) * bar_max_w if scale_max else 0
        out.append('<text class="bar-label" x="%.1f" y="%.1f" text-anchor="end" dominant-baseline="central">%s</text>' % (bar_x - 8, y + ROW_H / 2, esc(label)))
        out.append('<rect class="track" x="%.1f" y="%.1f" width="%.1f" height="%d" rx="4"/>' % (bar_x, y + (ROW_H - BAR_H) / 2, bar_max_w, BAR_H))
        out.append('<rect class="fill%s" x="%.1f" y="%.1f" width="%.1f" height="%d" rx="4"/>' % (" hl" if hl else "", bar_x, y + (ROW_H - BAR_H) / 2, fill_w, BAR_H))
        out.append('<text class="bar-value" x="%.1f" y="%.1f" dominant-baseline="central">%s%s</text>' % (bar_x + fill_w + 8, y + ROW_H / 2, fmt_num(v), esc(u or unit)))
    aria = esc(info) if info else "条形图"
    svg = ('<svg class="bars" viewBox="0 0 %d %d" width="%d" height="%d" style="width:min(100%%,%dpx);min-width:420px" role="img" aria-label="%s">%s</svg>'
           % (W, H, W, H, W, aria, "".join(out)))
    cap = "<figcaption>%s</figcaption>" % render_inline(info, ctx) if info else ""
    return '<figure class="diagram">%s%s</figure>' % (svg, cap)


# --------------------------------------------------------------------------- 整体
def parse_front_matter(text):
    meta = {}
    if text.startswith("---"):
        lines = text.split("\n")
        for j in range(1, min(len(lines), 40)):
            if lines[j].strip() == "---":
                for l in lines[1:j]:
                    m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$", l)
                    if m:
                        meta[m.group(1).lower()] = m.group(2).strip().strip('"').strip("'")
                return meta, "\n".join(lines[j + 1:]), j + 1
    return meta, text, 0


def detect_project():
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip()).name
    except Exception:
        pass
    return Path.cwd().name


def slugify(title, limit=40):
    s = re.sub(r"[^\w一-鿿㐀-䶿]+", "-", title, flags=re.U).strip("-")
    return (s[:limit].rstrip("-")) or "report"


def same_source(out, src):
    """这个 HTML 是不是由同一份稿子（或它留在旁边的副本）编译出来的：是就直接覆盖。"""
    if out.with_suffix(".md").resolve() == src:
        return True
    try:
        head = out.read_text(encoding="utf-8")[:3000]
    except OSError:
        return False
    m = re.search(r'<meta name="source" content="([^"]*)">', head)
    return bool(m) and _html.unescape(m.group(1)) == str(src)


def safe_name(s):
    return re.sub(r"[\\/:*?\"<>|\s]+", "-", s).strip("-") or "project"


def main():
    ap = argparse.ArgumentParser(description="把 Markdown 汇报稿编译成单文件 HTML", add_help=True)
    ap.add_argument("source", help="Markdown 源文件")
    ap.add_argument("--out", help="输出的 HTML 路径（默认放在报告目录里，按日期和标题命名；同一份稿子重编译会覆盖）")
    ap.add_argument("--out-dir", help="输出目录（默认 ~/.claude/visual-reports/<项目名>/，也可用环境变量 VISUAL_REPORT_DIR 改根目录）")
    ap.add_argument("--title", help="标题（默认取 front matter 的 title，或正文第一个一级标题）")
    ap.add_argument("--kind", choices=sorted(KIND_LABELS), help="plan=计划 result=结果 explain=解释 note=说明")
    ap.add_argument("--project", help="项目名（默认 git 仓库名或当前目录名）")
    ap.add_argument("--open", action="store_true", help="生成后用系统默认浏览器打开")
    ap.add_argument("--no-copy", action="store_true", help="不把 .md 源文件复制到输出目录")
    ap.add_argument("--quiet", action="store_true", help="只输出生成的路径")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    if not src.is_file():
        sys.exit("找不到源文件：%s" % src)
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\t", "    ")
    meta, body_text, skipped = parse_front_matter(text)
    lines = body_text.split("\n")

    title = args.title or meta.get("title")
    if not title:
        for i, l in enumerate(lines):
            m = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", l)
            if m:
                title = m.group(1).strip()
                del lines[i]
                break
    title = title or src.stem
    kind = (args.kind or meta.get("kind") or "note").lower()
    if kind not in KIND_LABELS:
        warn("不认识的 kind=%s，按 note 处理" % kind)
        kind = "note"
    project = args.project or meta.get("project") or detect_project()
    now = _dt.datetime.now()

    ctx = {"kind": kind, "base_dir": src.parent, "line_base": skipped}
    body_html = parse_blocks(lines, ctx)

    subtitle = meta.get("subtitle", "")
    meta_parts = ["<span>%s</span>" % esc(project), "<span>%s</span>" % now.strftime("%Y-%m-%d %H:%M")]
    if meta.get("author"):
        meta_parts.insert(0, "<span>%s</span>" % esc(meta["author"]))
    template = TEMPLATE.read_text(encoding="utf-8")
    page = (template.replace("{{TITLE}}", esc(title))
            .replace("{{KIND}}", kind)
            .replace("{{KIND_LABEL}}", KIND_LABELS[kind])
            .replace("{{SUBTITLE}}", '<p class="subtitle">%s</p>' % render_inline(subtitle, ctx) if subtitle else "")
            .replace("{{META}}", "".join(meta_parts))
            .replace("{{FOOT}}", "由 visual-report 生成 · %s · 源文件 %s" % (now.strftime("%Y-%m-%d %H:%M"), esc(src.name)))
            .replace("{{BODY}}", body_html))

    if args.out:
        out = Path(args.out).expanduser().resolve()
    else:
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else DEFAULT_BASE_DIR / safe_name(project)
        stem = "%s_%s" % (now.strftime("%Y-%m-%d"), slugify(title))
        out, k = out_dir / (stem + ".html"), 2
        while out.exists() and not same_source(out, src):
            out = out_dir / ("%s-%d.html" % (stem, k))
            k += 1
    page = page.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n<meta name="source" content="%s">' % esc(str(src)), 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    if not args.no_copy:
        md_copy = out.with_suffix(".md")
        if md_copy.resolve() != src:
            shutil.copyfile(src, md_copy)
    if not args.quiet:
        print("标题：%s（%s）  项目：%s  大小：%.0f KB" % (title, KIND_LABELS[kind], project, out.stat().st_size / 1024))
    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            warn("打不开浏览器（没有 %s 命令），请手动打开下面的文件" % opener)
    print(out)


if __name__ == "__main__":
    main()
