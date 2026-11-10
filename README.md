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
汇报里（报告和聊天摘要都算）不要频繁用简称、缩写或任务 / 计划代号（比如「P6」「A1」「第 2 步」「V、Q」），直接说清楚具体指的是什么；实在要反复提同一个东西，第一次写全称、括号里注明简称。
```

不加这段的话，Claude 只在它自己觉得合适、或者你说「汇报一下」「解释一下」「/visual-report」时才会用。

## 报告在哪

- Claude 桌面应用里：Claude 给你一个 `http://localhost:…` 链接（本机上一个只有你能连的小服务），点开就是渲染好的页面；有内置浏览器时会直接在里面打开。
- 终端里的 Claude Code：用系统浏览器打开。
- 远程机器 / 集群上：Claude 会起一个只有你能连的小服务，并给你一条 `ssh -fN -L …` 命令。在自己电脑上跑一次，之后 `http://localhost:<端口>/` 就能看这台机器上所有的报告（见下面「在集群上用」）。
- 文件存在项目自己的 `.claude/reports/` 里（git 会忽略），按日期和标题命名，旁边有 `.md` 源稿。想集中存放：环境变量 `VISUAL_REPORT_DIR`。

## 看不懂就选中问

通过报告服务的链接打开报告时，页面里自带一个小聊天框：

- 选中任何一段看不懂的文字，旁边会出现「这是什么？」。点一下，AI 就结合这份报告的原稿和项目里的代码，用大白话解释。
- 解释下面可以接着追问；在解释里又选中不懂的词，点「追问这个」。
- 右下角「问问这份报告」可以不选文字直接提问。
- 浮层挡住正文了，就按住它的标题栏拖开；拖边框或右下角可以改大小。拖过或拉过之后，它就钉在屏幕上：滚动页面不跟着走，点别处也不关（点 × 或按 Esc 关），这一页后面的提问也出现在那里。拉过的大小浏览器会记住，打开别的报告也是这个大小。

它在报告服务所在的机器上跑 `claude -p`：只读（只能看文件、搜文件），只在报告所属的项目目录里，最多同时 3 个，每个最多 5 分钟，关掉浮层就停掉还没答完的那次。需要那台机器上有登录过的 Claude Code：

- 集群上：装过并登录过 Claude Code 就行。
- 本机如果只装了 Claude 桌面应用：应用自带的 claude 单独运行时要另外登录一次，在终端里运行 `claude auth login`（`claude` 不在 PATH 上就用应用里那个的完整路径）。
- 想换个快一点的模型：`serve_reports.py start --model sonnet`，或者设环境变量 `VISUAL_REPORT_MODEL`。claude 不在常见位置时设 `VISUAL_REPORT_CLAUDE`。

接口要带页面里的 token，并且只接受 localhost 来的请求，别的网页没法借你的浏览器调用它。更新 skill 之后，下次生成报告时旧的报告服务会自动换成新版。

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
| ```` ```steps ```` | 带编号和状态的步骤：每步一行 `- [x] **标题**：说明`，状态 `[x]` `[~]` `[ ]` `[!]` `[-]` |
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
