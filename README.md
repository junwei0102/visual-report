# visual-report：让 agent 用一页带图的 HTML 向你汇报

一个 [Claude Code](https://docs.claude.com/en/docs/claude-code) skill。装上以后，Claude 在三种时刻会生成一页报告渲染给你看，而不是在聊天里堆一大段文字：

- **动手之前**：它理解的问题是什么、打算分几步、会动到哪里、需要你拍板什么（计划）
- **做完之后**：结论一句话、做了什么、怎么验证的、还有什么没做（结果）
- **你问为什么**：原因是什么、到底怎么回事、几种方案怎么选（解释）

报告长这样：开头一个「一句话结论」框；正文用大白话讲，术语第一次出现时鼠标悬停就有解释；关键机制配一张图（流程图、改前改后对比、带勾的步骤、条形图）；代码和日志折叠起来想看再点。浅色 / 深色模式和手机宽度都适配。

## 安装

```bash
git clone <这个仓库> ~/.claude/skills/visual-report
```

目录名必须是 `visual-report`。只需要 Python ≥ 3.8，不用装任何包。

要让 Claude **自动**在汇报时用它，在 `~/.claude/CLAUDE.md` 里加一段：

```markdown
## 汇报方式
向我汇报结果、说明打算怎么做、或者解释原因 / 方案时，用 visual-report skill 生成 HTML 报告（通俗解释 + 配图）给我看，聊天里只留几行摘要。
```

不加这段的话，Claude 只在它自己觉得合适、或者你说「汇报一下」「解释一下」「/visual-report」时才会用。

## 报告在哪

- Claude 桌面应用里：Claude 给你一个 `http://localhost:…` 链接（本机上一个只有你能连的小服务），点开就是渲染好的页面；有内置浏览器时会直接在里面打开。
- 终端里的 Claude Code：用系统浏览器打开。
- 远程机器 / 集群上：Claude 会起一个只有你能连的小服务，并给你一条 `ssh -fN -L …` 命令。在自己电脑上跑一次，之后 `http://localhost:<端口>/` 就能看这台机器上所有的报告（见下面「在集群上用」）。
- 文件存在项目自己的 `.claude/reports/` 里（git 会忽略），按日期和标题命名，旁边有 `.md` 源稿。想集中存放：环境变量 `VISUAL_REPORT_DIR`。

## 在集群上用

1. 把 skill 装到集群上：`ssh <集群> 'git clone https://github.com/junwei0102/visual-report.git ~/.claude/skills/visual-report'`，再在集群的 `~/.claude/CLAUDE.md` 里加上面那段「汇报方式」。
2. Claude 在集群上汇报时会打印一条形如 `ssh -fN -L 4xxxx:/run/user/<uid>/visual-report/server.sock <你>@<节点>` 的命令。在**自己电脑**的终端里跑一次（第一次要过 MFA）。
3. 打开它给的 `http://localhost:4xxxx/...` 链接。首页 `http://localhost:4xxxx/` 列出这台机器上所有报告。
4. 打不开：`pkill -f -- "-L 4xxxx:"` 关掉旧转发再重跑；报 `connect failed` 说明服务停了或换了节点，让 Claude 重新 `start`。原理和排错见 `references/remote-clusters.md`。

## 自己也能用

```bash
python3 ~/.claude/skills/visual-report/scripts/build_report.py 报告.md --kind result --open
```

Markdown 之外多了几个「组件」，都是围栏代码块：

| 写法 | 效果 |
|---|---|
| ```` ```tldr ```` | 开头的结论框 |
| ```` ```callout warn 标题 ```` | 提示框（`tip` `warn` `good` `bad` `note`） |
| ```` ```steps ```` | 带编号和状态的步骤，列表项开头 `[x]` `[~]` `[ ]` `[!]` |
| ```` ```compare ```` / ```` ```cards ```` | 用 `---` 分栏并排 |
| ```` ```details 标题 ```` | 默认折叠 |
| ```` ```kv ```` | `键: 值` 列表 |
| ```` ```flow 图注 ```` | 流程图：`A -> B -> C`，`B -> D : 箭头上的字`，`..>` 虚线，`{判断?}`，`((起止))`，`节点!` 高亮 |
| ```` ```bar 图注 ```` | 条形图：`名字: 数值` |
| `[[术语\|白话解释]]` | 悬停解释 |
| `==文字==` | 高亮 |

报告服务单独用：

```bash
python3 ~/.claude/skills/visual-report/scripts/serve_reports.py start   # 或 status / stop / url 某份报告.html
```

完整说明见 `SKILL.md`、`references/writing.md`（怎么写）、`references/diagrams.md`（怎么画）和 `references/remote-clusters.md`（集群上怎么看）。

## 改样式

所有样式在 `assets/template.html` 顶部的 `:root` 变量里：改一下 `--accent` 就能换主色，字体在 `--font-body`。
