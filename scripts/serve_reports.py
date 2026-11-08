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
只依赖 Python ≥ 3.8 标准库。
"""
import argparse
import getpass
import html as _html
import http.cookies
import ipaddress
import json
import mimetypes
import os
import re
import secrets
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


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "visual-report/1"

    def address_string(self):
        return "unix" if self.server.mode == "unix" else super().address_string()

    def log_message(self, fmt, *args):
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

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        self.server.touch()
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path) or "/"
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
            ctype = "text/html; charset=utf-8"
        elif suffix in (".md", ".txt", ".log"):
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_text(200, target.read_bytes(), ctype, head)


class ReportServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mode, bind, token):
        self.mode, self.token = mode, token
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

    def resolve(self, path):
        """/<项目>/<文件名> → 登记表里对应的文件；没登记的一律不给。"""
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2:
            return None
        project, name = parts
        for e in load_registry(prune=False):
            if e.get("project") == project and Path(e.get("path", "")).name == name:
                target = Path(e["path"])
                return target if target.is_file() else None
        return None

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
    token = None if mode == "unix" else load_token()
    server = ReportServer(mode, bind, token)
    info = {"mode": mode, "pid": os.getpid(), "port": opts.port,
            "host": socket.gethostname(), "socket": socket_path() if mode == "unix" else None,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"), "idleHours": opts.idle_hours}
    info.update(node_details() if is_remote() else {"remote": False})
    write_state(info)
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


def ensure_running(listen=None, port=None, idle_hours=12.0):
    """服务没在跑就启动；返回 server.json 里的信息。"""
    mode = listen or ("unix" if is_remote() else "tcp")
    port = port or default_port()
    info = read_state()
    if info and server_alive(info):
        if info["mode"] == mode and (port is None or info["port"] == port):
            return info
        try:  # 跑着的是另一种模式 / 另一个端口：换掉
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
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    for _ in range(50):
        time.sleep(0.2)
        info = read_state()
        if info and server_alive(info):
            return info
    raise SystemExit("报告服务没起来，看看日志：%s" % LOG_FILE)


def print_banner(info, report=None, prefix="   ", project=None):
    """给用户看的说明：链接、转发命令、打不开怎么办。agent 把这些原样转给用户。"""
    if report:
        print(prefix + "报告： " + url_for(info, report, project))
    print(prefix + "所有报告的目录： " + index_url(info))
    if not info.get("remote"):
        return
    node = info.get("node") or info["host"]
    print(prefix + "服务跑在 %s 上（Unix socket，只有你本人能连）。在【你自己电脑】的终端里跑一次下面这条，"
          "之后 %s 上所有报告都走这条转发，服务重启也不用重做；已经在跑就跳过：" % (node, node))
    print(prefix + "  " + forward_command(info))
    if not info.get("nodeAddress") and not info.get("loginAddress"):
        print(prefix + "（<登录地址> 换成你平时 ssh 登录这里用的名字，或者在这台机器上设好 VISUAL_REPORT_SSH_HOST。）")
    print(prefix + "第一次可能要过一次二次验证（MFA）、确认一次主机指纹。")
    print(prefix + "打不开的话：")
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
        info = ensure_running(opts.listen, opts.port, opts.idle_hours)
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
