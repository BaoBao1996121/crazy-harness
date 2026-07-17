# 伪代码模板

```text
on process restart:
    rebuild mailbox from ______
    rebuild waits from ______
    rebuild operation states from ______

if operation == STARTED and no terminal record:
    mark ______
    release execution slot
    request ______

on matching event:
    wake exactly one ______
    ack delivery after ______
```
