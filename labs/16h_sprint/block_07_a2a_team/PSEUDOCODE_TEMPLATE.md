# 伪代码模板

```text
on root event:
    coordinator 查询 ______
    assignment = goal + exit criteria + authority + budget
    durable_mailbox.send(assignment)

worker needs peer evidence:
    request = brief + artifact_refs + intent + scope + depth + cost
    if peer_policy.authorize(request, contract):
        ______
    else:
        ______

reviewer receives only ______, never worker full transcript
```
