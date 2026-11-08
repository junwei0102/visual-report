# 图：什么时候画、用哪种、怎么写

## 先决定要不要画

一张图值得画，是因为它让一个不了解上下文的人**看到一个机制**，而这个机制用文字要绕好几句：数据从哪流到哪、哪一步出了错、改前改后差在哪条边上、几个方案的差别在哪里。
如果你画出来的图只是几个名词框加没有字的箭头，那它不如一句话。宁可不画。

一份报告通常一张图就够，最多两张。每张图下面写一句图注：这张图在说什么、箭头 / 颜色 / 虚线各代表什么。

## 用哪种

| 想说明的 | 用 | 要点 |
|---|---|---|
| 流程、调用链、数据流、状态怎么变 | `flow` | 箭头上写「做什么」；高亮出问题或要改的那一步 |
| 改前 vs 改后、方案 A vs B | `compare`（文字）或两张小 `flow` | 让人能指着说「区别在这里」 |
| 做了哪些事、计划分几步 | `steps` | 用 `[x] [~] [ ]` 标状态，一眼看出进度 |
| 几个数字比大小：测试数、耗时、大小 | `bar` | 高亮最该看的那一根 |
| 文件结构、目录树 | 普通代码块 | 不用画 |
| 时间线 | `steps` 或表格 | 顺序本身是信息时才用 steps |
| 别的任何形状 | 手写 SVG | 见最后一节 |

## flow 的完整语法

```
```flow 图注写在这里，会显示在图下面
direction: TB            # 可选。默认横排 LR；竖排写 TB
A -> B -> C              # 一条链，实线箭头
B -> D : 箭头上的字       # 「: 文字」跟在箭头指向的节点后面
C ..> E                  # 虚线：可选路径、没走到的路径、异步
D => F                   # 粗的强调线：重点路径
((开始)) -> 处理 -> ((结束))   # 双括号：圆角起止框
处理 -> {成功了吗?}       # 花括号：判断框
{成功了吗?} -> 写结果! : 是   # 节点名后面加 ! ：高亮这个框
{成功了吗?} -> 重试 : 否
重试 -> 处理              # 回头的箭头会自动画成弧线
读 CSV\n(1 分钟) -> 清洗   # \n 手动换行；太长的标签也会自动折成两三行
```
```

规则：

- 节点靠**文字**识别：两行里写了同样的文字就是同一个框。所以 `{成功了吗?}` 第二次出现也要带花括号，或者至少文字一样。
- 布局是自动的：按箭头方向分层，每层内按连线少交叉排。想调位置，就调节点在文字里出现的先后顺序，或者换方向。
- 横排超过 5 个节点就会太宽，缩到看不清。改 `direction: TB`、用 `\n` 把长标签折短、或者拆成两张图。脚本会在图太宽时给出警告。
- 跨好几层的长箭头可能压到中间的框。实在难看就拆图，或者在中间加一个过渡节点。
- 标签里不要用 `->`、`:` 后面跟空格这类会被当成语法的写法；时间 `12:30` 没有空格，不受影响。

## bar 的完整语法

```
```bar 图注
max: 100       # 可选，不写就按最大值撑满
unit: %        # 可选，统一的单位；也可以每行自己带：通过: 42 个
通过: 42 !     # 行尾 ! 高亮
失败: 3
跳过: 7.5
```
```

数值可以带千分位逗号和小数。负数能解析但画不出来，别用。

## 嵌图片

`![这张图在说什么](path/to/plot.png)` 单独一行，图片会被读成 base64 直接嵌进 HTML（单文件、离线可看），文字变成图注。路径相对于 `.md` 文件所在目录或当前目录。
matplotlib 画的图、截图都这样放。超过 8MB 的图不会嵌入。

## 手写 SVG

`flow` 和 `bar` 画不出来的形状（时间轴、分层架构带分组框、坐标图），直接在 Markdown 里写 `<svg>`，单独成段。要点：

- `viewBox="0 0 W H"` 按内容定大小，再加 `width="W" height="H" style="max-width:100%;height:auto"`。
- **取色用页面变量**，深色模式才正常：线和字用 `stroke="currentColor"` / `fill="currentColor"`；重点用 `var(--accent)`；好坏用 `var(--good)` `var(--bad)` `var(--warn)`；框的底色用 `var(--surface)` 或 `var(--surface-2)`；浅线用 `var(--line)`。不要写死 `#333`、`white` 这类颜色。
- 字号 11–13px，短标签；解释性的句子写在图注里，不塞进图里。
- 箭头用 `<marker>`，id 在这一张图里唯一（一页里可能有好几张图）。
- 对齐到网格：同一行的框同一个 y，间距一致。手画的图之所以看着「业余」，九成是因为没对齐。
- 外面包一层 `<figure class="diagram"> … <figcaption>图注</figcaption></figure>`，就和自动画的图一个样式。
- 脚本会用 XML 解析器检查你的 SVG，标签没闭合、属性没加引号会在 stderr 里提示。

最小例子：

```html
<figure class="diagram">
<svg viewBox="0 0 420 120" width="420" height="120" style="max-width:100%;height:auto" role="img" aria-label="三层架构">
  <defs><marker id="arch-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>
  <rect x="10" y="10" width="400" height="100" rx="10" fill="none" stroke="var(--line)" stroke-dasharray="4 3"/>
  <text x="20" y="28" font-size="11" fill="var(--fg-muted)">服务端</text>
  <rect x="30" y="45" width="100" height="40" rx="8" fill="var(--surface-2)" stroke="currentColor"/>
  <text x="80" y="70" font-size="13" text-anchor="middle" fill="currentColor">API</text>
  <rect x="290" y="45" width="100" height="40" rx="8" fill="var(--accent-soft)" stroke="var(--accent)"/>
  <text x="340" y="70" font-size="13" text-anchor="middle" fill="var(--accent-strong)">数据库</text>
  <line x1="130" y1="65" x2="288" y2="65" stroke="currentColor" stroke-width="1.5" marker-end="url(#arch-arrow)"/>
  <text x="209" y="58" font-size="11" text-anchor="middle" fill="var(--fg-muted)">每次请求查一次</text>
</svg>
<figcaption>高亮的数据库是这次要加缓存的地方：现在每个请求都直接查它。</figcaption>
</figure>
```

可用的页面变量：`--fg` `--fg-muted` `--bg` `--surface` `--surface-2` `--line` `--line-strong` `--accent` `--accent-strong` `--accent-soft` `--good` `--good-soft` `--warn` `--warn-soft` `--bad` `--bad-soft` `--note` `--note-soft`。
