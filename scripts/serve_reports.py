#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve_reports.py —— 用一个只有你本人能连的小服务把 visual-report 生成过的报告挂出来，
在远程机器 / HPC 集群上经 SSH 端口转发，在自己电脑的浏览器里看。

    python3 serve_reports.py start [--listen unix|tcp] [--port N] [--idle-hours 12]
    python3 serve_reports.py url 某份报告.html     # 打印这份报告的链接 + 转发命令（服务没在跑就顺手启动）
    python3 serve_reports.py status | stop

服务不按目录服务，而是按「登记表」：build_report.py 每生成一份报告就记到
~/.claude/visual-reports/registry.json（路径、标题、项目），首页按项目列出所有报告，
只有登记过的文件能被访问。url 命令会把没登记过的报告顺手登记上。

远程（有 SSH_CONNECTION / CC_CLUSTER）默认监听一个 0600 权限的 Unix socket，登录节点上别的用户连不上；
用户在自己电脑上跑一次 `ssh -fN -L <端口>:<socket> <用户>@<节点>`，之后所有报告都走这条转发，
服务重启也不用重做（转发是按需连 socket 的）。本地默认监听 127.0.0.1 的 TCP 端口，链接里带 token。
端口按「用户 + 集群 + 节点」算出来，稳定不变，所以转发命令和书签一直有效。

报告页面里可以直接提问：服务发页面时注入一个小聊天框（assets/chat.html）。用户选中看不懂的文字，
服务就在报告所属的项目目录里跑一次只读的 `claude -p`，用大白话解释，答案流式显示，还能追问。
需要这台机器上有登录过的 Claude Code。提问框标题栏的 ⚙ 能换回答用的模型和思考力度（记在浏览器里）；
`start --model sonnet` 或环境变量 VISUAL_REPORT_MODEL 改的是 ⚙ 里「默认」用的模型，VISUAL_REPORT_CLAUDE 指定 claude 的路径。接口要带 token，并且只认 localhost 来的请求。
只依赖 Python ≥ 3.8 标准库。
"""
import argparse
import getpass
import hmac
import html as _html
import http.cookies
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "visual-report"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "assets" / "template.html"
REGISTRY = Path(os.environ.get("VISUAL_REPORT_REGISTRY", Path.home() / ".claude" / "visual-reports" / "registry.json"))
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP
STATE_FILE = CACHE_DIR / "server.json"
LOG_FILE = CACHE_DIR / "server.log"
TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP / "token"
KIND_LABELS = {"plan": "计划", "result": "结果", "explain": "解释", "note": "说明"}
VERSION = 4                # 服务的版本：ensure_running 发现跑着的是旧版本就重启，新功能才会生效
CHAT_SNIPPET = HERE.parent / "assets" / "chat.html"
MAX_PARALLEL_JOBS = 3      # 最多同时跑几个 AI 解释子进程
JOB_TIMEOUT = 300          # 一次回答最多跑几秒
JOB_KEEP = 1800            # 答完的回答在内存里留多久（秒）
MAX_SOURCE_CHARS = 40000   # 报告原稿最多放多少字进提示词
EXPLAINER_TOOLS = "Read,Grep,Glob"
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # --effort 认的几档思考力度
MODEL_RE = re.compile(r"^(?:(?:agy|claude):)?(?:[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,79})?$")  # 页面选的模型：可带 agy:/claude: 前缀
DENIED_TOOLS = "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch"
# 父 Claude Code / Antigravity 会话设的变量，嵌套子进程不能继承
PARENT_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_SSE_PORT",
    "ANTIGRAVITY_AGENT", "ANTIGRAVITY_TRAJECTORY_ID", "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_SOURCE_METADATA", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN",
    "ANTIGRAVITY_PROJECT_ID", "AGY_REQUIRE_AUTHENTICATED_UI_BOOTSTRAP",
)
CLAUDE_MODEL_OPTIONS = [
    ["claude:", "Claude Code 默认"],
    ["claude:fable", "Fable（最强，最慢）"],
    ["claude:opus", "Opus（较强）"],
    ["claude:sonnet", "Sonnet（较快）"],
    ["claude:haiku", "Haiku（最快）"],
]
AGY_FALLBACK_MODELS = [
    {"id": "gemini-4-neon", "label": "Gemini 4 Neon", "default": "gemini-4-neon-high",
     "variants": {"low": "gemini-4-neon-low", "medium": "gemini-4-neon-medium", "high": "gemini-4-neon-high", "xhigh": "gemini-4-neon-xhigh"}},
    {"id": "gemini-4-carbon", "label": "Gemini 4 Carbon", "default": "gemini-4-carbon-medium",
     "variants": {"low": "gemini-4-carbon-low", "medium": "gemini-4-carbon-medium", "high": "gemini-4-carbon-high"}},
    {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "default": "gemini-4-argon-medium",
     "variants": {"low": "gemini-4-argon-low", "medium": "gemini-4-argon-medium", "high": "gemini-4-argon-high"}},
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "default": "gemini-3.8-flash-medium",
     "variants": {"low": "gemini-3.8-flash-low", "medium": "gemini-3.8-flash-medium", "high": "gemini-3.8-flash-high"}},
]
CLAUDE_SHORTS = {"fable", "opus", "sonnet", "haiku"}


# --------------------------------------------------------------------------- 环境
def is_remote():
    return any(os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "CC_CLUSTER"))


def default_port():
    """每个「用户 + 节点」一个固定端口（40000–59999），和 diff-review 的 20000 段错开。"""
    if os.environ.get("VISUAL_REPORT_PORT"):
        return int(os.environ["VISUAL_REPORT_PORT"])
    if not is_remote():
        return 40000 + os.getuid() % 20000
    node = "%s/%s" % (os.environ.get("CC_CLUSTER", ""), socket.gethostname().split(".")[0])
    return 40000 + (os.getuid() + zlib.crc32(node.encode("utf-8"))) % 20000


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit("不敢用 %s：它不是你自己的目录" % path)
    os.chmod(path, 0o700)
    return path


def socket_dir():
    """放 Unix socket 的、只有本用户能进的节点本地目录。"""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        return private_dir(Path(base) / APP)
    return private_dir("/tmp/%s-%d" % (APP, os.getuid()))


def socket_path():
    return str(socket_dir() / "server.sock")


def load_token():
    if TOKEN_FILE.is_file():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    private_dir(TOKEN_FILE.parent)
    tok = secrets.token_urlsafe(18)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def is_global_ip(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def is_local_ip(ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    probe = socket.socket(fam, socket.SOCK_DGRAM)
    try:
        probe.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def node_address():
    """这台机器能从外面直接 ssh 到的地址；没有就返回 None（那就得经登录地址跳过来）。
    思路和 diff-review 一样：试本机的完整域名、去掉一段之后的名字、以及当前 SSH 会话连进来的 IP，
    只有解析出的地址是公网地址并且确实配在本机网卡上才算数。"""
    fqdn, short = socket.getfqdn(), socket.gethostname().split(".")[0]
    domain = fqdn.split(".")[1:] if fqdn.split(".")[0] == short else []
    names = [fqdn]
    if len(domain) >= 3:
        names += [".".join([short] + domain[:i] + domain[i + 1:]) for i in range(len(domain))]
    names += [".".join([short] + domain[i:]) for i in range(1, len(domain) - 1)]
    for name in dict.fromkeys(names):
        if name.endswith(".arpa"):
            continue
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(name, None)}
        except OSError:
            continue
        if any(is_global_ip(ip) and is_local_ip(ip) for ip in addresses):
            return name
    for ip in (os.environ.get("SSH_CONNECTION") or "").split()[2:3]:
        if is_global_ip(ip) and is_local_ip(ip):
            try:
                name = socket.gethostbyaddr(ip)[0]
                if ip in {entry[4][0] for entry in socket.getaddrinfo(name, None)}:
                    return name
            except OSError:
                pass
            return ip
    return None


def login_address():
    for key in ("VISUAL_REPORT_SSH_HOST", "DIFF_REVIEW_SSH_HOST"):
        if os.environ.get(key):
            return os.environ[key]
    cluster = os.environ.get("CC_CLUSTER")
    return "%s.alliancecan.ca" % cluster if cluster else None


def node_details():
    return {"remote": True, "user": getpass.getuser(), "node": socket.gethostname().split(".")[0],
            "nodeAddress": node_address(), "loginAddress": login_address()}


def forward_command(info):
    port = info["port"]
    target = info["socket"] if info["mode"] == "unix" else "localhost:%d" % port
    user = info.get("user") or getpass.getuser()
    if info.get("nodeAddress"):
        return "ssh -fN -L %d:%s %s@%s" % (port, target, user, info["nodeAddress"])
    login = info.get("loginAddress") or "<登录地址>"
    jump = login if "@" in login or login.startswith("<") else "%s@%s" % (user, login)
    return "ssh -fN -J %s -L %d:%s %s@%s" % (jump, port, target, user, info.get("node") or info.get("host"))


# --------------------------------------------------------------------------- 状态
def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(info):
    private_dir(CACHE_DIR)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def server_alive(info):
    if not info or info.get("host") != socket.gethostname() or not pid_alive(info.get("pid")):
        return False
    if info["mode"] == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        addr = info["socket"]
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        addr = ("127.0.0.1", info["port"])
    s.settimeout(1)
    try:
        s.connect(addr)
        return True
    except OSError:
        return False
    finally:
        s.close()


def base_url(info):
    return "http://localhost:%d" % info["port"]


def load_registry(prune=True):
    try:
        entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if prune:
        entries = [e for e in entries if Path(e.get("path", "")).is_file()]
    return entries


def save_registry(entries):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def describe_report(path):
    """从 HTML 开头读出标题和类型；项目名按它所在的位置猜（<项目>/.claude/reports/x.html）。"""
    path = Path(path).resolve()
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        head = ""
    m = re.search(r"<title>(.*?)</title>", head, re.S)
    k = re.search(r'class="kind (\w+)"', head)
    parent = path.parent
    project = parent.parent.parent.name if parent.name == "reports" and parent.parent.name == ".claude" else parent.name
    return {"path": str(path), "title": _html.unescape(m.group(1).strip()) if m else path.stem,
            "kind": k.group(1) if k else "note", "project": project,
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)) if path.is_file() else ""}


def ensure_registered(file, project=None):
    """没登记过的报告顺手登记上（给了 project 就按它归类）；返回它的登记项。"""
    path = str(Path(file).resolve())
    entries = load_registry()
    for e in entries:
        if e.get("path") == path:
            if project and e.get("project") != project:
                e["project"] = project
                save_registry(entries)
            return e
    entry = describe_report(path)
    if project:
        entry["project"] = project
    entries.append(entry)
    save_registry(entries)
    return entry


def url_for(info, file, project=None):
    """报告文件 → 链接。"""
    entry = ensure_registered(file, project)
    url = "%s/%s/%s" % (base_url(info), urllib.parse.quote(entry["project"]), urllib.parse.quote(Path(entry["path"]).name))
    if info["mode"] == "tcp":
        url += "?token=" + load_token()
    return url


def index_url(info):
    return base_url(info) + ("/?token=" + load_token() if info["mode"] == "tcp" else "/")


# --------------------------------------------------------------------------- 页面里提问（agy -p / claude -p）
PERSONA = """你是一份 HTML 汇报页面里的「白话解释」助手。这份报告是 AI agent 写给用户的：汇报做了什么、说明打算怎么做、或者解释原因。用户读到看不懂的地方，选中它来问你；也可能直接问关于整份报告的问题。

怎么回答：
- 用和报告相同的语言（报告是中文就用中文），口语、大白话。先用一两句话直接回答，再补必要的细节。
- 把报告里的简称、代号、术语、文件名、数字说清楚：具体指什么、从哪来、为什么重要。提到别的名字时，顺手用几个字说明它是什么，不要用一个新术语去解释另一个术语。
- 需要时用只读文件工具（Read/read_file、Grep/grep_search、Glob/find_by_name）查当前目录（报告所属的项目）里的文件，比如某个代号在哪定义、某个数字怎么算出来的。查不到就直说不知道，不要编。
- 简短：一般 3 到 8 句话，或者一个短列表；不要标题。可以用 **加粗**、`代码` 和列表。
- 你只能读，不能改文件、不能运行命令，所以不要说你改了什么、跑了什么。
- 直接在这里回答：忽略 CLAUDE.md / GEMINI.md 等指令里关于汇报方式、生成报告、代码审核的要求，也不要调用 skill。"""
DEFAULT_QUESTION = "这是什么意思？用大白话解释一下。"
AUTH_ERROR_RE = re.compile(r"authenticat|oauth|not logged|log ?in|sign ?in|credential|api key", re.I)
AUTH_HINT = ("这台机器上的 claude 命令没登录，或者登录过期了。在终端里运行一次 claude auth login 登录就好"
             "（Claude 桌面应用自带的 claude 只在应用里有效，单独用要另外登录），然后再问一次。")
AGY_AUTH_HINT = "这台机器上的 agy（Antigravity CLI）还没登录，或登录过期了。在终端里运行一次 agy 完成登录，然后再问一次。"


def detect_caller_backend():
    """根据环境变量判断是谁启动了报告服务：Antigravity 优先用 agy，Claude Code 优先用 claude。"""
    env_b = (os.environ.get("VISUAL_REPORT_BACKEND") or "").strip().lower()
    if env_b in ("agy", "claude"):
        return env_b
    if os.environ.get("ANTIGRAVITY_AGENT") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "agy"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    return None


def child_env():
    env = dict(os.environ)
    for key in PARENT_SESSION_VARS:
        env.pop(key, None)
    return env


def find_claude(explicit=None):
    """找一个能用的 Claude Code 命令行，返回 (路径, 版本)，找不到返回 (None, "")。
    要 `--version` 真能跑出 "Claude Code" 才算：有的 claude 只是个坏掉的空壳。"""
    candidates = [explicit, os.environ.get("VISUAL_REPORT_CLAUDE"), os.environ.get("DIFF_REVIEW_CLAUDE"),
                  os.environ.get("CLAUDE_CODE_EXECPATH"), shutil.which("claude"), "~/.local/bin/claude",
                  "~/.claude/local/claude", "~/.npm-global/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    seen = set()
    for cand in candidates:
        if not cand:
            continue
        path = os.path.expanduser(cand)
        if path in seen or not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        seen.add(path)
        try:
            out = subprocess.run([path, "--version"], env=child_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=60).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if "claude code" in out.lower():
            return path, out.strip()
    return None, ""


def find_agy(explicit=None):
    """找一个能用的 Antigravity CLI（agy），返回 (路径, help_text) 或 (None, "")。"""
    candidates = [explicit, os.environ.get("VISUAL_REPORT_AGY"), os.environ.get("DIFF_REVIEW_AGY"),
                  os.environ.get("AGY_BIN"), shutil.which("agy"), "~/.gemini/bin/agy",
                  "~/.local/bin/agy", "/opt/homebrew/bin/agy", "/usr/local/bin/agy"]
    seen = set()
    for cand in candidates:
        if not cand:
            continue
        path = os.path.expanduser(cand)
        if path in seen or not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        seen.add(path)
        try:
            out = subprocess.run([path, "--help"], env=child_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=20).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if "--print" in out and "--output-format" in out:
            return path, out
    return None, ""


def parse_agy_models(text):
    """把 `agy models` 输出解析成 (grouped_models, raw_labels, auth_needed)。
    同一模型的 -low/-medium/-high/-xhigh 变体合并成一条，交由「思考力度」下拉框选择。"""
    clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")
    if "please sign in" in clean.lower():
        return [], {}, True
    raw_labels = {}
    groups = {}
    order = []
    for raw_line in clean.splitlines():
        line = raw_line.split("\r")[-1].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\t+|\s{2,})(.+?)\s*$", line)
        if not m:
            continue
        mid, label = m.group(1), m.group(2)
        raw_labels[mid] = label
        em = re.match(r"^(.+)-(low|medium|high|xhigh)$", mid)
        if em:
            base, eff = em.group(1), em.group(2)
            base_label = re.sub(r"\s*\((?:Low|Medium|High|XHigh)\)\s*$", "", label, flags=re.I)
            if base not in groups:
                groups[base] = {"id": base, "label": base_label, "default": mid, "variants": {}}
                order.append(base)
            groups[base]["variants"][eff] = mid
        else:
            if mid not in groups:
                groups[mid] = {"id": mid, "label": label, "default": mid, "variants": {}}
                order.append(mid)
    return [groups[k] for k in order], raw_labels, False


def output_lines(proc, give_up):
    """子进程 stdout 的一行行输出，直到它关掉 stdout，或者 give_up() 为真（每秒查一次）。
    不用 readline 死等：卡住的进程、或被杀后还占着管道的孙进程，会让它永远等下去。"""
    fd, rest = proc.stdout.fileno(), b""
    while not give_up():
        if not select.select([fd], [], [], 1.0)[0]:
            continue
        chunk = os.read(fd, 65536)
        lines = (rest + chunk).split(b"\n")
        rest = lines.pop()
        for line in lines:
            yield line
        if not chunk:
            if rest:
                yield rest
            return


def describe_tool(block, root):
    """解释时查了什么，给页面显示「看了 xxx」。"""
    name, args = block.get("name") or "tool", block.get("input") or {}

    def rel(p):
        p = str(p or "")
        return os.path.relpath(p, root) if p.startswith(root + os.sep) else p

    if name in ("Read", "read_file", "view_file"):
        return {"tool": "read", "arg": rel(args.get("file_path") or args.get("Path") or args.get("AbsolutePath"))}
    if name in ("Grep", "grep_search"):
        return {"tool": "grep", "arg": str(args.get("pattern") or args.get("Query") or "")}
    if name in ("Glob", "find_by_name", "list_dir"):
        return {"tool": "glob", "arg": str(args.get("pattern") or args.get("Pattern") or rel(args.get("DirectoryPath")))}
    return {"tool": name, "arg": ""}


def kill_group(proc, sig=signal.SIGTERM):
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def stop_proc(proc):
    """确保子进程结束：先 SIGTERM，5 秒后还在就 SIGKILL。"""
    if proc.poll() is not None:
        return
    kill_group(proc)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        kill_group(proc, signal.SIGKILL)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


class Job:
    """一次提问：子进程的输出一边流进 text，页面按 offset 轮询取新增的部分。"""

    def __init__(self, prompt, cwd, backend, model=None, effort=None):
        self.id = secrets.token_hex(8)
        self.prompt, self.cwd = prompt, cwd
        self.backend, self.model, self.effort, self.model_used = backend, model, effort, None
        self.text, self.tools = "", []
        self.done, self.error, self.cancelled = False, None, False
        self.proc, self.finished = None, None


class Explainer:
    """用 agy -p（Antigravity / Gemini）或 claude -p（Claude Code）回答页面里的提问：只读，限定在项目目录里，最多同时 3 个。"""

    def __init__(self, explicit_claude=None, model=None, explicit_agy=None, backend=None):
        self.explicit_claude = explicit_claude
        self.explicit_agy = explicit_agy
        self.preferred_backend = backend or detect_caller_backend()
        self.model = model or os.environ.get("VISUAL_REPORT_MODEL") or None
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(MAX_PARALLEL_JOBS)
        self.jobs = {}
        self.claude_bin, self.claude_version, self.claude_caps, self.claude_error = None, "", {}, None
        self.agy_bin, self.agy_caps, self.agy_error = None, {}, None
        self.agy_models = list(AGY_FALLBACK_MODELS)
        self.agy_labels = {}
        self.checked = threading.Event()
        self.settings_file = str(CACHE_DIR / "explainer-settings.json")
        threading.Thread(target=self.detect, daemon=True).start()

    def detect(self):
        try:
            self._detect_agy()
            self._detect_claude()
        finally:
            self.checked.set()

    def _detect_agy(self):
        try:
            path, help_text = find_agy(self.explicit_agy)
            self.agy_bin = path
            if not path:
                self.agy_error = "这台机器上找不到 agy（Antigravity CLI）。"
                return
            self.agy_caps = {flag: flag in help_text for flag in (
                "--disable-slash-commands", "--sandbox", "--effort", "--model")}
            self.agy_error = None
            self._refresh_agy_models()
        except (OSError, subprocess.SubprocessError) as e:
            self.agy_error = "检查 agy 命令时出错：%s" % e

    def _refresh_agy_models(self):
        if not self.agy_bin:
            return
        try:
            out = subprocess.run([self.agy_bin, "models"], env=child_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=25).stdout.decode("utf-8", "replace")
            models, labels, auth_needed = parse_agy_models(out)
            if auth_needed:
                self.agy_error = AGY_AUTH_HINT
                return
            if models:
                self.agy_models = models
                self.agy_labels = labels
            if self.agy_error == AGY_AUTH_HINT:
                self.agy_error = None
        except (OSError, subprocess.SubprocessError):
            pass

    def _detect_claude(self):
        try:
            self.claude_bin, self.claude_version = find_claude(self.explicit_claude)
            if not self.claude_bin:
                self.claude_error = ("这台机器上找不到能用的 claude 命令（Claude Code）。装好、登录以后重启报告服务"
                                     "（serve_reports.py stop 再 start），或者设环境变量 VISUAL_REPORT_CLAUDE 指向它。")
                return
            help_text = subprocess.run([self.claude_bin, "--help"], env=child_env(), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, timeout=60).stdout.decode("utf-8", "replace")
            self.claude_caps = {flag: flag in help_text for flag in (
                "--include-partial-messages", "--tools", "--disallowedTools", "--no-session-persistence",
                "--strict-mcp-config", "--settings", "--effort")}
            self.claude_caps["dontAsk"] = "dontAsk" in help_text
            private_dir(CACHE_DIR)
            Path(self.settings_file).write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
            self.claude_error = None
            self.check_claude_auth()
        except (OSError, subprocess.SubprocessError) as e:
            self.claude_error = "检查 claude 命令时出错：%s" % e

    def check_claude_auth(self):
        """claude auth status 说没登录，就先把原因记下来；老版本没有这个命令时不管，问的时候再说。"""
        if not self.claude_bin:
            return
        try:
            out = subprocess.run([self.claude_bin, "auth", "status", "--json"], env=child_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=60).stdout.decode("utf-8", "replace")
            logged_in = json.loads(out or "{}").get("loggedIn")
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            return
        if logged_in is False:
            self.claude_error = AUTH_HINT
        elif self.claude_error == AUTH_HINT:
            self.claude_error = None

    def backend_ready(self, name):
        if name == "agy":
            return bool(self.agy_bin) and not self.agy_error
        if name == "claude":
            return bool(self.claude_bin) and not self.claude_error
        return False

    def default_backend(self):
        m = (self.model or "").strip()
        if m.startswith("agy:"):
            return "agy" if self.backend_ready("agy") else ("claude" if self.backend_ready("claude") else "agy")
        if m.startswith("claude:"):
            return "claude" if self.backend_ready("claude") else ("agy" if self.backend_ready("agy") else "claude")
        for cand in (self.preferred_backend, "agy", "claude"):
            if cand and self.backend_ready(cand):
                return cand
        return self.preferred_backend or ("agy" if self.agy_bin else "claude")

    def model_groups(self):
        """给前端下拉框用的分组模型列表。"""
        groups = []
        agy_opts = [["agy:", "Antigravity 默认"]] + [["agy:" + m["id"], m["label"]] for m in self.agy_models]
        if self.backend_ready("agy"):
            groups.append({"backend": "agy", "label": "Antigravity (Gemini)", "options": agy_opts})
        if self.backend_ready("claude"):
            groups.append({"backend": "claude", "label": "Claude Code", "options": list(CLAUDE_MODEL_OPTIONS)})
        if self.default_backend() == "claude" and len(groups) == 2:
            groups.reverse()
        return groups

    def status(self):
        if not self.checked.is_set():
            return {"available": None, "error": None}
        agy_ok, claude_ok = self.backend_ready("agy"), self.backend_ready("claude")
        any_ok = agy_ok or claude_ok
        default_b = self.default_backend()
        err = None
        if not any_ok:
            parts = [e for e in (self.agy_error if self.preferred_backend != "claude" else None, self.claude_error) if e]
            err = "；".join(parts) or "这台机器上找不到可用的 agy 或 claude 命令。"
        can_effort = (bool(self.agy_caps.get("--effort")) if default_b == "agy" else bool(self.claude_caps.get("--effort")))
        if agy_ok and claude_ok:
            can_effort = bool(self.agy_caps.get("--effort")) or bool(self.claude_caps.get("--effort"))
        labels = dict(self.agy_labels)
        for m in self.agy_models:
            labels.setdefault(m["id"], m["label"])
        return {
            "available": any_ok,
            "error": err,
            "backend": default_b,
            "backends": {
                "agy": {"available": agy_ok, "error": self.agy_error},
                "claude": {"available": claude_ok, "version": self.claude_version, "error": self.claude_error},
            },
            "version": ("Antigravity CLI" if default_b == "agy" else self.claude_version),
            "model": self.model,
            "canSetEffort": can_effort,
            "models": self.model_groups(),
            "modelLabels": labels,
        }

    def resolve_target(self, model=None, effort=None):
        """解析页面/启动参数传进来的 model + effort → (backend, resolved_model, effort)。"""
        spec = (model if model is not None else self.model) or ""
        spec = spec.strip()
        backend = None
        raw = spec
        if spec.startswith("agy:"):
            backend, raw = "agy", spec[4:]
        elif spec.startswith("claude:"):
            backend, raw = "claude", spec[7:]
        elif spec:
            agy_ids = {m["id"] for m in self.agy_models} | set(self.agy_labels)
            if spec in agy_ids or spec.startswith(("gemini", "gpt-oss")):
                backend = "agy"
            elif spec in CLAUDE_SHORTS or spec.startswith("claude"):
                backend = "claude" if self.backend_ready("claude") else ("agy" if spec in agy_ids else "claude")
        if not backend:
            backend = self.default_backend()
        if backend == "agy" and raw:
            for m in self.agy_models:
                if raw == m["id"]:
                    variants = m.get("variants") or {}
                    if effort and variants:
                        if effort in variants:
                            raw = variants[effort]
                        elif effort == "max":
                            raw = variants.get("xhigh") or variants.get("high") or m["default"]
                        else:
                            raw = m["default"]
                    else:
                        raw = m["default"]
                    break
        return backend, raw or None, effort or None

    def command_claude(self, model=None, effort=None):
        caps = self.claude_caps
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose"]
        if caps.get("--include-partial-messages"):
            cmd.append("--include-partial-messages")
        if caps.get("--tools"):
            cmd += ["--tools", EXPLAINER_TOOLS]
        if caps.get("--disallowedTools"):
            cmd += ["--disallowedTools", DENIED_TOOLS]
        if caps.get("dontAsk"):  # 默认不放行的（比如读项目目录以外的文件）一律拒绝
            cmd += ["--permission-mode", "dontAsk"]
        if caps.get("--no-session-persistence"):
            cmd.append("--no-session-persistence")
        if caps.get("--strict-mcp-config"):
            cmd.append("--strict-mcp-config")
        if caps.get("--settings"):
            cmd += ["--settings", self.settings_file]
        if model:
            cmd += ["--model", model]
        if effort and caps.get("--effort"):
            cmd += ["--effort", effort]
        return cmd

    def command_agy(self, prompt, model=None, effort=None):
        caps = self.agy_caps
        cmd = [self.agy_bin, "--output-format", "stream-json"]
        if caps.get("--disable-slash-commands"):
            cmd.append("--disable-slash-commands")
        if caps.get("--sandbox"):
            cmd.append("--sandbox")
        if model and caps.get("--model"):
            cmd += ["--model", model]
        if effort and caps.get("--effort"):
            cmd += ["--effort", effort]
        cmd += ["--print", prompt]
        return cmd

    def submit(self, prompt, cwd, model=None, effort=None):
        self.checked.wait(90)
        if self.claude_bin and not os.path.isfile(self.claude_bin):  # 桌面应用升级后，旧版本自带的 claude 可能被删了：重新找
            self._detect_claude()
        elif self.claude_error == AUTH_HINT:  # 之前没登录：每次提问前再查一次，登录以后不用重启服务
            self.check_claude_auth()
        if self.agy_bin and self.agy_error == AGY_AUTH_HINT:
            self._refresh_agy_models()
        backend, resolved_model, resolved_effort = self.resolve_target(model, effort)
        if not self.backend_ready(backend):
            err = self.agy_error if backend == "agy" else self.claude_error
            raise RuntimeError(err or self.status().get("error") or "AI 命令还在检查中，过几秒再试")
        if backend == "claude" and not self.claude_caps.get("--effort"):
            resolved_effort = None
        if backend == "agy" and not self.agy_caps.get("--effort"):
            resolved_effort = None
        job = Job(prompt, cwd, backend, resolved_model, resolved_effort)
        with self.lock:
            now = time.time()
            for key in [k for k, j in self.jobs.items() if j.done and now - (j.finished or now) > JOB_KEEP]:
                del self.jobs[key]
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job is not None:
            job.cancelled = True
            proc = job.proc
            if proc is not None:
                kill_group(proc)

    def stop_all(self):
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            job.cancelled = True
            if job.proc is not None:
                stop_proc(job.proc)

    def _append(self, job, text):
        with self.lock:
            job.text += text

    def _finish(self, job, error=None):
        with self.lock:
            job.error, job.done, job.finished, job.proc = error, True, time.time(), None

    def _run(self, job):
        with self.slots:
            if job.cancelled:
                return self._finish(job)
            try:
                self._exec(job)
            except Exception as e:  # 不能让页面一直转圈
                self._finish(job, "解释出错：%s" % e)

    def _exec(self, job):
        if job.backend == "agy":
            return self._exec_agy(job)
        return self._exec_claude(job)

    def _exec_agy(self, job):
        cmd = self.command_agy(job.prompt, job.model, job.effort)
        proc = subprocess.Popen(cmd, cwd=job.cwd, env=child_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        job.proc = proc
        errors = []
        reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        reader.start()
        deadline = time.time() + JOB_TIMEOUT
        final = error = None
        has_text = False
        last_text_step = None
        for raw in output_lines(proc, give_up=lambda: time.time() > deadline or job.cancelled):
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "init":
                init_info = ev.get("init") or {}
                if init_info.get("model"):
                    with self.lock:
                        job.model_used = str(init_info["model"])
            elif kind == "step_update":
                su = ev.get("step_update") or {}
                stype = su.get("step_type")
                if stype == "tool" and su.get("state") == "ACTIVE":
                    tinfo = su.get("tool_info") or {}
                    block = {"name": tinfo.get("name") or su.get("tool_name") or "tool",
                             "input": tinfo.get("parameters") or {}}
                    with self.lock:
                        job.tools.append(describe_tool(block, job.cwd))
                elif stype == "agent_response" and su.get("text_delta"):
                    idx = su.get("step_index")
                    if has_text and last_text_step is not None and idx != last_text_step:
                        self._append(job, "\n\n")
                    has_text = True
                    last_text_step = idx
                    self._append(job, su["text_delta"])
            elif kind == "result":
                res = ev.get("result") or {}
                if res.get("status") != "SUCCESS":
                    error = str(res.get("error") or res.get("status") or "未知错误")
                else:
                    final = res.get("response") or ""
                break
        stop_proc(proc)
        reader.join(2)
        if job.cancelled:
            return self._finish(job)
        if final and not has_text:
            self._append(job, final)
        if final is None and error is None:
            if time.time() > deadline:
                error = "%d 分钟还没答完，已经停掉了，可以再问一次。" % (JOB_TIMEOUT // 60)
            else:
                tail = b"".join(x for x in errors if x).decode("utf-8", "replace").strip()[-800:]
                error = "agy 中途退出了（exit %s）%s" % (proc.returncode, "：" + tail if tail else "")
        if error and AUTH_ERROR_RE.search(error):
            error = "%s\n原始报错：%s" % (AGY_AUTH_HINT, error)
        self._finish(job, error)

    def _exec_claude(self, job):
        proc = subprocess.Popen(self.command_claude(job.model, job.effort), cwd=job.cwd, env=child_env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        job.proc = proc
        errors = []
        reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        reader.start()
        try:
            proc.stdin.write(job.prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        deadline = time.time() + JOB_TIMEOUT
        final = error = None
        has_text = False
        partial = self.claude_caps.get("--include-partial-messages")
        for raw in output_lines(proc, give_up=lambda: time.time() > deadline or job.cancelled):
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init" and ev.get("model"):
                with self.lock:
                    job.model_used = str(ev["model"])
            elif kind == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "message_start" and has_text:
                    self._append(job, "\n\n")
                elif e.get("type") == "content_block_delta":
                    delta = e.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        has_text = True
                        self._append(job, delta["text"])
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        with self.lock:
                            job.tools.append(describe_tool(block, job.cwd))
                    elif block.get("type") == "text" and block.get("text") and not partial:
                        self._append(job, ("\n\n" if has_text else "") + block["text"])
                        has_text = True
            elif kind == "result":
                if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                    error = str(ev.get("result") or ev.get("subtype") or "未知错误")
                else:
                    final = ev.get("result") or ""
                break  # 最后一条事件：不用等进程慢慢退出
        stop_proc(proc)
        reader.join(2)
        if job.cancelled:
            return self._finish(job)
        if final and not has_text:
            self._append(job, final)
        if final is None and error is None:
            if time.time() > deadline:
                error = "%d 分钟还没答完，已经停掉了，可以再问一次。" % (JOB_TIMEOUT // 60)
            else:
                tail = b"".join(x for x in errors if x).decode("utf-8", "replace").strip()[-800:]
                error = "claude 中途退出了（exit %s）%s" % (proc.returncode, "：" + tail if tail else "")
        if error and AUTH_ERROR_RE.search(error):
            error = "%s\n原始报错：%s" % (AUTH_HINT, error)
        self._finish(job, error)


def clip(s, n):
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def report_source(html_path):
    """报告的 Markdown 原稿：优先用旁边那份 .md，其次 <meta name="source"> 指的文件，都没有就从 HTML 里抠正文。"""
    path = Path(html_path)
    try:
        page = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        page = ""
    candidates = [path.with_suffix(".md")]
    m = re.search(r'<meta name="source" content="([^"]*)"', page[:8000])
    if m:
        candidates.append(Path(_html.unescape(m.group(1))))
    for cand in candidates:
        try:
            if cand.is_file():
                return cand.read_text(encoding="utf-8", errors="replace")[:MAX_SOURCE_CHARS], str(cand)
        except OSError:
            pass
    body = re.search(r"<article[^>]*>(.*?)</article>", page, re.S)
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", body.group(1) if body else page, flags=re.S)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", text)[:MAX_SOURCE_CHARS], None


def project_dir(entry):
    """解释时的当前目录：登记时记下的项目根目录；老报告没记，就按 <项目>/.claude/reports/ 的位置猜。"""
    root = entry.get("root")
    if root and os.path.isdir(root):
        return root
    parent = Path(entry["path"]).resolve().parent
    if parent.name == "reports" and parent.parent.name == ".claude":
        return str(parent.parent.parent)
    return str(parent)


def build_prompt(entry, body):
    source, src_path = report_source(entry["path"])
    cwd = project_dir(entry)
    parts = [PERSONA, "", "# 这份报告", "标题：%s" % entry.get("title", ""), "HTML 文件：%s" % entry["path"]]
    if src_path:
        parts.append("Markdown 原稿：%s" % src_path)
    parts += ["项目目录（也是你的当前目录）：%s" % cwd, "",
              "下面是报告原稿。页面由它编译而来：[[术语|解释]] 是悬停解释，==文字== 是高亮，```steps 这类是页面组件。",
              "<report>", source, "</report>", ""]
    selection = clip(body.get("selection"), 2000)
    if selection:
        parts += ["# 用户选中的文字", "「%s」" % selection]
        if body.get("context"):
            parts.append("它所在的段落：%s" % clip(body.get("context"), 2000))
        if body.get("heading"):
            parts.append("它所在的小节：%s" % clip(body.get("heading"), 200))
        parts.append("")
    history = [h for h in (body.get("history") or []) if isinstance(h, dict)]
    if history:
        parts.append("# 之前的对话")
        for h in history[-8:]:
            parts += ["用户：%s" % clip(h.get("q"), 2000), "你：%s" % clip(h.get("a"), 6000)]
        parts.append("")
    parts += ["# 用户现在的问题", clip(body.get("question"), 2000) or DEFAULT_QUESTION]
    return "\n".join(parts), cwd


_SNIPPET = {"mtime": None, "text": ""}


def chat_snippet():
    try:
        mtime = CHAT_SNIPPET.stat().st_mtime
    except OSError:
        return ""
    if _SNIPPET["mtime"] != mtime:
        _SNIPPET.update(mtime=mtime, text=CHAT_SNIPPET.read_text(encoding="utf-8"))
    return _SNIPPET["text"]


def inject_chat(page, report_path, token):
    """发报告页面时注入「选中提问」的小聊天框：旧报告不用重新编译也有；直接打开 HTML 文件时没有它。"""
    snippet = chat_snippet()
    if not snippet:
        return page
    cfg = json.dumps({"token": token, "report": report_path}, ensure_ascii=False).replace("</", "<\\/")
    block = "<script>window.__VR__ = %s;</script>\n%s\n" % (cfg, snippet)
    i = page.lower().rfind("</body>")
    return page[:i] + block + page[i:] if i >= 0 else page + block


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "visual-report/%d" % VERSION
    QUIET = ("/api/job", "/api/ping")  # 页面轮询，不写日志

    def address_string(self):
        return "unix" if self.server.mode == "unix" else super().address_string()

    def log_message(self, fmt, *args):
        if self.path.startswith(self.QUIET):
            return
        sys.stderr.write("%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.address_string(), fmt % args))

    def send_text(self, code, text, ctype="text/plain; charset=utf-8", head=False, extra=None):
        data = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_text(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def host_ok(self):
        """只认 localhost / 127.0.0.1 / [::1]：挡住 DNS rebinding（别的网站把自己的域名解析到 127.0.0.1，
        借你的浏览器来读页面、调接口）。"""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[:host.find("]") + 1] if host.startswith("[") else host.split(":", 1)[0]
        return name in ("localhost", "127.0.0.1", "[::1]")

    def api_token_ok(self):
        """接口要带 X-VR-Token 头：别的网页发不了带自定义头的跨站请求，也读不到页面里的 token。"""
        given = self.headers.get("X-VR-Token", "")
        return bool(given) and hmac.compare_digest(given.encode("utf-8"), self.server.api_token.encode("utf-8"))

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path) or "/"
        if not self.host_ok():
            return self.send_text(403, "只能用 localhost 打开，比如 http://localhost:端口/", head=head)
        if path.startswith("/api/"):
            return self.api_get(path, urllib.parse.parse_qs(parsed.query))
        self.server.touch()
        if self.server.token:  # TCP 模式：第一次用带 ?token= 的链接进来，之后靠 cookie
            given = urllib.parse.parse_qs(parsed.query).get("token", [None])[0]
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            have = jar["vr"].value if "vr" in jar else None
            if given == self.server.token:
                self.send_response(302)
                self.send_header("Location", urllib.parse.quote(path))
                self.send_header("Set-Cookie", "vr=%s; Path=/; HttpOnly; SameSite=Strict" % given)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if have != self.server.token:
                return self.send_text(403, "需要 token：请用 build_report.py --serve 或 serve_reports.py url 打印的完整链接（带 ?token=）打开。", head=head)
        if path == "/":
            return self.send_text(200, self.server.index_html(), "text/html; charset=utf-8", head)
        target = self.server.resolve(path)
        if target is None:
            return self.send_text(404, "没有这个文件：%s" % path, head=head)
        suffix = target.suffix.lower()
        if suffix in (".html", ".htm"):
            page = target.read_text(encoding="utf-8", errors="replace")
            return self.send_text(200, inject_chat(page, path, self.server.api_token), "text/html; charset=utf-8", head)
        if suffix in (".md", ".txt", ".log"):
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_text(200, target.read_bytes(), ctype, head)

    def api_get(self, path, query):
        if not self.api_token_ok():
            return self.send_json({"error": "缺少或错误的 token"}, 401)
        self.server.touch()
        explainer = self.server.explainer
        if path == "/api/ping":
            return self.send_json({"ok": True, "version": VERSION, "ai": explainer.status()})
        if path == "/api/job":
            job = explainer.get((query.get("id") or [""])[0])
            if job is None:
                return self.send_json({"error": "这个回答已经过期了，再问一次吧"}, 404)
            try:
                since = max(0, int((query.get("since") or ["0"])[0]))
            except ValueError:
                since = 0
            with explainer.lock:
                text, done, error, tools = job.text, job.done, job.error, list(job.tools)
                model, effort = job.model_used, job.effort
            return self.send_json({"text": text[since:], "offset": len(text), "done": done,
                                   "error": error, "tools": tools[-3:], "model": model, "effort": effort})
        return self.send_json({"error": "没有这个接口"}, 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.host_ok():
            return self.send_json({"error": "只能用 localhost 访问"}, 403)
        if not path.startswith("/api/"):
            return self.send_json({"error": "没有这个接口"}, 404)
        if not self.api_token_ok():
            return self.send_json({"error": "缺少或错误的 token"}, 401)
        self.server.touch()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 300000:
            return self.send_json({"error": "请求太大"}, 413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            return self.send_json({"error": "请求不是合法的 JSON"}, 400)
        if not isinstance(body, dict):
            body = {}
        if path == "/api/ask":
            return self.handle_ask(body)
        if path == "/api/cancel":
            self.server.explainer.cancel(str(body.get("job") or ""))
            return self.send_json({"ok": True})
        return self.send_json({"error": "没有这个接口"}, 404)

    def handle_ask(self, body):
        entry = self.server.resolve_entry(str(body.get("report") or ""))
        if entry is None:
            return self.send_json({"error": "找不到这份报告：它不在登记表里，或者文件被删了"}, 404)
        if not str(body.get("question") or "").strip() and not str(body.get("selection") or "").strip():
            return self.send_json({"error": "问题是空的"}, 400)
        model = str(body.get("model") or "").strip()
        effort = str(body.get("effort") or "").strip()
        if model and not MODEL_RE.match(model):
            return self.send_json({"error": "模型名不对：%s" % clip(model, 40)}, 400)
        if effort and effort not in EFFORTS:
            return self.send_json({"error": "思考力度只能是 %s 之一" % "、".join(EFFORTS)}, 400)
        prompt, cwd = build_prompt(entry, body)
        try:
            job = self.server.explainer.submit(prompt, cwd, model or None, effort or None)
        except RuntimeError as e:
            return self.send_json({"error": str(e)}, 503)
        return self.send_json({"job": job.id})


class ReportServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mode, bind, token, api_token, explainer):
        self.mode, self.token, self.api_token, self.explainer = mode, token, api_token, explainer
        self.last_activity = time.time()
        if mode == "unix":
            self.address_family = socket.AF_UNIX
        super().__init__(bind, Handler)

    def server_bind(self):
        if self.mode == "unix":
            try:
                os.unlink(self.server_address)
            except FileNotFoundError:
                pass
            old = os.umask(0o177)
            try:
                self.socket.bind(self.server_address)
            finally:
                os.umask(old)
            os.chmod(self.server_address, 0o600)
            self.server_name, self.server_port = "localhost", 0
        else:
            super().server_bind()

    def touch(self):
        self.last_activity = time.time()

    def resolve_entry(self, path):
        """/<项目>/<文件名> → 登记表里对应的那一项；没登记的、文件不在了的，一律不给。"""
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2:
            return None
        project, name = parts
        for e in load_registry(prune=False):
            if e.get("project") == project and Path(e.get("path", "")).name == name:
                return e if Path(e["path"]).is_file() else None
        return None

    def resolve(self, path):
        entry = self.resolve_entry(path)
        return Path(entry["path"]) if entry else None

    def index_html(self):
        return render_index(self.mode)


def grouped_reports():
    """[(项目, [登记项...])...]，项目和项目内都按最新在前排。"""
    groups = {}
    for e in load_registry():
        groups.setdefault(e.get("project", "?"), []).append(e)
    for files in groups.values():
        files.sort(key=lambda e: e.get("time", ""), reverse=True)
    return sorted(groups.items(), key=lambda kv: kv[1][0].get("time", ""), reverse=True)


def render_index(mode):
    groups = grouped_reports()
    parts = []
    total = sum(len(files) for _, files in groups)
    if not groups:
        parts.append("<p>还没有登记过的报告。agent 用 visual-report 汇报后，这里会按项目列出来。</p>")
    for project, files in groups:
        parts.append("<h2>%s</h2>" % _html.escape(project))
        rows = []
        for e in files:
            href = "/%s/%s" % (urllib.parse.quote(project), urllib.parse.quote(Path(e["path"]).name))
            rows.append('<tr><td><a href="%s">%s</a><div class="meta">%s</div></td><td>%s</td><td class="num">%s</td></tr>' % (
                href, _html.escape(e.get("title", "")), _html.escape(str(Path(e["path"]).parent)),
                _html.escape(KIND_LABELS.get(e.get("kind", ""), "")), _html.escape(e.get("time", ""))))
        parts.append('<div class="table-wrap"><table><thead><tr><th>报告</th><th>类型</th><th>时间</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows))
    template = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.is_file() else "<!doctype html><title>{{TITLE}}</title><body>{{BODY}}</body>"
    where = "%s 上" % socket.gethostname().split(".")[0] if is_remote() else "本机"
    return (template.replace("{{TITLE}}", "汇报目录")
            .replace("{{KIND}}", "note").replace("{{KIND_LABEL}}", "目录")
            .replace("{{SUBTITLE}}", '<p class="subtitle">%s所有 visual-report 生成过的报告，最新的在最前面</p>' % where)
            .replace("{{META}}", "<span>%d 份报告</span><span>%s</span>" % (total, time.strftime("%Y-%m-%d %H:%M")))
            .replace("{{FOOT}}", "visual-report 报告服务 · %s · 空闲一段时间后会自动退出，再次运行 start 即可" % ("Unix socket" if mode == "unix" else "127.0.0.1"))
            .replace("{{BODY}}", "\n".join(parts)))


# --------------------------------------------------------------------------- 服务进程
def run_server(opts):
    """前台跑服务（start 会把它放到后台）。"""
    mode = opts.listen
    bind = socket_path() if mode == "unix" else ("127.0.0.1", opts.port)
    api_token = load_token()  # 页面里提问的接口，两种模式都要它
    token = None if mode == "unix" else api_token  # 打开页面要不要 token：只有本地 TCP 要，Unix socket 靠文件权限
    explainer = Explainer(opts.claude, opts.model, opts.agy, opts.backend)
    server = ReportServer(mode, bind, token, api_token, explainer)
    info = {"mode": mode, "pid": os.getpid(), "port": opts.port, "version": VERSION,
            "host": socket.gethostname(), "socket": socket_path() if mode == "unix" else None,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"), "idleHours": opts.idle_hours,
            "model": opts.model, "claude": opts.claude, "agy": opts.agy, "backend": opts.backend}
    info.update(node_details() if is_remote() else {"remote": False})
    write_state(info)

    def record_ai():  # 查完 agy / claude 命令能不能用，记进 server.json，print_banner 据此提示
        explainer.checked.wait(120)
        info["ai"] = explainer.status()
        state = read_state()
        if state and state.get("pid") == os.getpid():
            write_state(info)

    threading.Thread(target=record_ai, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *a: stop.set())
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    sys.stderr.write("%s serving registry %s on %s (pid %d)\n" % (info["started"], REGISTRY, bind, os.getpid()))
    try:
        while not stop.is_set():
            stop.wait(30)
            if opts.idle_hours > 0 and time.time() - server.last_activity > opts.idle_hours * 3600:
                sys.stderr.write("idle for %.1f h, shutting down\n" % opts.idle_hours)
                break
    finally:
        explainer.stop_all()
        server.shutdown()
        server.server_close()
        if mode == "unix":
            try:
                os.unlink(bind)
            except OSError:
                pass
        state = read_state()
        if state and state.get("pid") == os.getpid():
            try:
                STATE_FILE.unlink()
            except OSError:
                pass


def ensure_running(listen=None, port=None, idle_hours=12.0, claude=None, model=None, agy=None, backend=None):
    """服务没在跑就启动；跑着的是旧版本、另一种模式、另一个端口或别的后端/模型，就换掉。返回 server.json 里的信息。"""
    mode = listen or ("unix" if is_remote() else "tcp")
    port = port or default_port()
    wanted_backend = backend or detect_caller_backend()
    info = read_state()
    if info and server_alive(info):
        same = info["mode"] == mode and info["port"] == port and info.get("version") == VERSION
        if (model is not None and info.get("model") != model) or (claude is not None and info.get("claude") != claude):
            same = False
        if (agy is not None and info.get("agy") != agy) or (wanted_backend is not None and info.get("backend") != wanted_backend):
            same = False
        if same:
            return info
        try:  # 换掉
            os.kill(info["pid"], signal.SIGTERM)
            for _ in range(20):
                if not pid_alive(info["pid"]):
                    break
                time.sleep(0.2)
        except OSError:
            pass
    private_dir(CACHE_DIR)
    log = open(LOG_FILE, "a")
    args = [sys.executable, str(Path(__file__).resolve()), "_run", "--listen", mode,
            "--port", str(port), "--idle-hours", str(idle_hours)]
    if model:
        args += ["--model", model]
    if claude:
        args += ["--claude", claude]
    if agy:
        args += ["--agy", agy]
    if wanted_backend:
        args += ["--backend", wanted_backend]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    for _ in range(50):
        time.sleep(0.2)
        info = read_state()
        if info and server_alive(info):
            return info
    raise SystemExit("报告服务没起来，看看日志：%s" % LOG_FILE)


def print_banner(info, report=None, prefix="   ", project=None):
    """给用户看的说明：链接、转发命令、打不开怎么办。agent 把这些原样转给用户。
    远程时转发命令每次都要带上：用户电脑上的转发会断（睡眠、重启、断网），agent 看不到。"""
    if info.get("remote"):
        print(prefix + "【给 agent】这是远程机器：在聊天里给链接时，把下面几行原样转给用户，转发命令每次都带上，哪怕之前给过。")
    if report:
        print(prefix + "报告： " + url_for(info, report, project))
    print(prefix + "所有报告的目录： " + index_url(info))
    ai = info.get("ai") or {}
    if ai.get("available") is False:
        print(prefix + "页面里的提问功能现在用不了：%s" % ai.get("error"))
    else:
        print(prefix + "看不懂的地方：在页面里选中它，点「这是什么？」就有 AI 的白话解释，还能接着追问；右下角「问问这份报告」可以直接提问。")
    if not info.get("remote"):
        return
    node = info.get("node") or info["host"]
    print(prefix + "服务跑在 %s 上（Unix socket，只有你本人能连），要经 SSH 转发才能打开上面的链接。" % node)
    print(prefix + "还没开转发、或者链接打不开（电脑睡眠、重启后转发会断），就在【你自己电脑】的终端里跑一次下面这条；"
          "开着的话不用再跑，%s 上所有报告共用这一条，服务重启也不用重做：" % node)
    print(prefix + "  " + forward_command(info))
    if not info.get("nodeAddress") and not info.get("loginAddress"):
        print(prefix + "（<登录地址> 换成你平时 ssh 登录这里用的名字，或者在这台机器上设好 VISUAL_REPORT_SSH_HOST。）")
    print(prefix + "第一次可能要过一次二次验证（MFA）、确认一次主机指纹。")
    print(prefix + "跑了还是打不开的话：")
    print(prefix + "  · ssh 报 Address already in use：本地端口被旧的转发占着，先 pkill -f -- \"-L %d:\" 再重跑上面那条。" % info["port"])
    print(prefix + "  · ssh 报 connect failed / open failed：服务停了或换了节点，让 agent 重新 start，用它新给的命令。")
    print(prefix + "  · 总在 %s 上跑 Claude 的话，可以把 ssh config 固定到这个节点并自动带上转发（见 references/remote-clusters.md）。" % node)


def main():
    ap = argparse.ArgumentParser(description="visual-report 的报告浏览服务")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "_run"):
        p = sub.add_parser(name)
        p.add_argument("--listen", choices=("unix", "tcp"), default=None, help="默认：远程 unix，本地 tcp")
        p.add_argument("--port", type=int, default=None, help="本地端口（也是转发用的端口），默认按用户和节点算一个固定值")
        p.add_argument("--idle-hours", type=float, default=12, help="多久没人访问就退出（0 = 不退出）")
        p.add_argument("--backend", choices=("agy", "claude"), default=None, help="默认问答后端（agy=Antigravity/Gemini，claude=Claude Code；默认按调用环境自动识别）")
        p.add_argument("--model", default=None, help="页面里提问默认用的模型，比如 agy:gemini-4-neon 或 claude:sonnet；页面 ⚙ 里另选了就用那个")
        p.add_argument("--claude", default=None, help="claude 命令的路径（默认自动找，或环境变量 VISUAL_REPORT_CLAUDE）")
        p.add_argument("--agy", default=None, help="agy（Antigravity CLI）的路径（默认自动找，或环境变量 VISUAL_REPORT_AGY）")
    u = sub.add_parser("url", help="打印一份报告的链接和转发命令")
    u.add_argument("file", nargs="+")
    u.add_argument("--project", help="登记时归到哪个项目下（默认按文件位置猜）")
    sub.add_parser("status")
    sub.add_parser("stop")
    opts = ap.parse_args()

    if opts.cmd == "_run":
        opts.listen = opts.listen or ("unix" if is_remote() else "tcp")
        opts.port = opts.port or default_port()
        return run_server(opts)
    if opts.cmd == "start":
        info = ensure_running(opts.listen, opts.port, opts.idle_hours, opts.claude, opts.model, opts.agy, opts.backend)
        print("报告服务在跑（pid %d，%s）" % (info["pid"], "Unix socket " + info["socket"] if info["mode"] == "unix" else "127.0.0.1:%d" % info["port"]))
        return print_banner(info)
    if opts.cmd == "url":
        info = ensure_running()
        for f in opts.file[:-1]:
            print("   报告： " + url_for(info, f, opts.project))
        return print_banner(info, report=opts.file[-1], project=opts.project)
    info = read_state()
    alive = server_alive(info)
    if opts.cmd == "status":
        if not alive:
            print("报告服务没有在跑。启动：python3 %s start" % Path(__file__).name)
            return
        print("报告服务在跑（pid %d，自 %s，登记表 %s）" % (info["pid"], info.get("started", "?"), REGISTRY))
        return print_banner(info)
    if opts.cmd == "stop":
        if not alive:
            print("报告服务本来就没在跑。")
            return
        os.kill(info["pid"], signal.SIGTERM)
        for _ in range(25):
            if not pid_alive(info["pid"]):
                break
            time.sleep(0.2)
        print("已停止（pid %d）。" % info["pid"])


if __name__ == "__main__":
    main()
