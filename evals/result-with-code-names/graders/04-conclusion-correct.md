---
type: llm
focus: trace
weight: 1
---

The conclusion is correct: full fine-tuning beats training only the classifier head, and because adding augmentation and additionally halving the learning rate differ by only 0.2 points, the plan's rule picks the faster one (augmentation without the halved learning rate).
