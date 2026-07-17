# 伪代码模板

```text
contract = coordinator 给出的 ______
plan = agent 对 contract 的 ______

每轮编译 context:
    replace protected goal slot
    replace protected exit criteria slot
    replace latest local plan slot

if 模型请求提交:
    findings = 校验 ______ + ______ + ______
    if findings 非空且 nudge_budget > 0:
        ______
    elif findings 非空:
        ______
    else:
        交给 reviewer
```
