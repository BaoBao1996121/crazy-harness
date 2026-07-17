# 伪代码模板

```text
for item in candidate_context:
    if item 是受保护 slot:
        ______
    elif item 是低价值旧噪声:
        ______
    elif item 超过阈值:
        写 ArtifactStore，active view 放 ______

if model 请求 read_full(ref):
    校验 ______
    inline 原文，lease = ______

每轮结束:
    lease = ______
    到期后 representation = ______
```
