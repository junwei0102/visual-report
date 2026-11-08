---
tags: [visual-report]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

我们训练脚本的 checkpoint 现在存在集群的本地盘上，同事建议改存到对象存储（S3 那种）。我不太懂这块，你给我解释一下两种方式到底差在哪、各有什么坑，然后说说你建议怎么选。我们一次训练大概存 20 个 checkpoint，每个 8GB，训练节点偶尔会被抢占。
