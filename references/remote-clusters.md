# 在远程机器和 HPC 集群上看报告

## 原理

集群上没有浏览器，桌面应用的侧栏也够不到那里的文件。所以 `build_report.py --serve`（或 `serve_reports.py start`）会在那台机器上起一个很小的 HTTP 服务：

- 远程（检测到 `SSH_CONNECTION` 或 `CC_CLUSTER`）时不开 TCP 端口，而是监听一个 Unix socket：`$XDG_RUNTIME_DIR/visual-report/server.sock`，没有这个变量时用 `/tmp/visual-report-<uid>/server.sock`。socket 权限 0600、目录 0700，登录节点上的其他用户连不上。
- 用户在自己电脑上跑一条 `ssh -fN -L <端口>:<socket> <用户>@<节点>`，浏览器打开 `http://localhost:<端口>/…` 就通了。转发是按需连 socket 的，服务重启、换一份报告都不用重做；换了节点才要重做。
- 端口按「用户 + 集群 + 节点」算出来（40000–59999 段，和 diff-review 的 20000 段错开），稳定不变，所以书签和转发命令一直有效。环境变量 `VISUAL_REPORT_PORT` 可以指定。
- 服务只给看**登记过**的报告：`build_report.py` 每生成一份就记到 `~/.claude/visual-reports/registry.json`，`serve_reports.py url 某份.html` 也会顺手登记。首页 `http://localhost:<端口>/` 按项目列出全部报告。
- 本地（没有 SSH 会话）时监听 `127.0.0.1` 的 TCP 端口，链接里带一个 token（存在 `~/.config/visual-report/token`），防止本机别的程序乱看。
- 12 小时没人访问就自动退出（`--idle-hours` 可调，0 = 不退出），下次 `--serve` 会自动再起。

**socket 只存在于服务所在的那个节点上**，下面的转发问题几乎都源于此。

## 转发命令怎么来的

集群的登录地址（比如 `fir.alliancecan.ca`）背后有好几个登录节点，新开的 SSH 连接不一定落在服务所在的节点，所以打印的命令直接指向那个节点：

```bash
# 节点有公网地址（脚本验证过这个地址确实指向本机）
ssh -fN -L <端口>:<socket> <用户>@<节点的公网地址>

# 节点没有公网地址（计算节点、或只有内网名字）：经登录地址跳过去
ssh -fN -J <用户>@<登录地址> -L <端口>:<socket> <用户>@<节点名>
```

登录地址：Alliance 集群按 `CC_CLUSTER` 推出 `<集群>.alliancecan.ca`；别的机器在那台机器上设环境变量 `VISUAL_REPORT_SSH_HOST`（也认 `DIFF_REVIEW_SSH_HOST`）。

`-fN` 让转发在后台常驻。第一次可能要过一次二次验证（MFA）、确认一次主机指纹。取消：`pkill -f -- "-L <端口>:"`。

## 固定登录节点

如果你总在同一个节点上跑 Claude（比如 tmux 一直开着），可以把 ssh config 里这个集群的 `Host` 段固定到这个节点并自动带上转发，以后 `ssh <别名>` 就自带转发，同一个链接一直能用。写法和 diff-review 完全一样（见 diff-review skill 的 `references/remote-clusters.md`「固定登录节点」），只是多加一行：

```
  LocalForward <visual-report 的端口> <socket 路径>
```

两个 skill 的转发可以写在同一个 `Host` 段里。

## 排错

| 现象 | 处理 |
|---|---|
| ssh 报 `connect failed: No such file or directory` / `open failed` | 转发落到了别的节点，或服务已退出。在集群上跑 `serve_reports.py status`（没在跑就 `start`），用它打印的命令重做转发 |
| ssh 报 `bind [127.0.0.1]:<端口>: Address already in use` | 本地端口被旧转发占着：`pkill -f -- "-L <端口>:"` 后重跑。还占着就 `lsof -nP -iTCP:<端口> -sTCP:LISTEN` 看是谁 |
| 页面 403「需要 token」 | 只有本地 TCP 模式才有 token；用 `--serve` 打印的完整链接（带 `?token=`），进过一次之后靠 cookie |
| 页面 404 | 这份报告没登记（比如是手动复制过去的文件）：`serve_reports.py url 它的路径` 登记并拿链接 |
| 想在 VS Code / Cursor 的 Remote-SSH 里看 | 它们只自动转发 TCP 端口：`serve_reports.py start --listen tcp`，然后在 Ports 面板转发那个端口。同节点其他用户也能连到这个端口，只靠 token 挡着 |
| 日志 | `~/.cache/visual-report/server.log` |
