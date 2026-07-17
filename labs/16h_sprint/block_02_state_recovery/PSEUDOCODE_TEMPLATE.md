# 伪代码模板

```text
events = 读取当前 assignment 的事实
if 存在未解决的外部操作:
    ______

turn = ______
if turn 已有持久化 response:
    response = ______
else:
    response = 调模型并 ______

command = ______
if command 非法:
    ______
else:
    ______
```
