# Bug Card: Hook patch 绕过授权

## 现象

原始参数合法，pre-tool Hook 把路径改到工作区外，系统仍执行。

## 不变量

Hook 的输出是不可信的新输入：必须重新做 schema 与 Hard Policy；Hook 永远不能修改 agent、assignment、mode 或权限集合。

## 诊断提示

按时间顺序写出 original call、patched call、二次 validation 和 policy context，检查授权到底作用在哪个对象上。
