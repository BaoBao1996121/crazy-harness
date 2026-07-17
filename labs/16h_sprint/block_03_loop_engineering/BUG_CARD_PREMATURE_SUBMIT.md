# Bug Card: 提前交作业

## 现象

输出 JSON 长得正确，但测试证据缺失或还有 `UNKNOWN` 操作，系统仍允许提交。

## 不变量

CompletionGate 是机械门：`schema 合法 AND 每项证据存在 AND 无未决操作` 才能进入 Reviewer。

## 诊断提示

先列出三类 finding，再检查代码是否因为第一项通过而提前返回。修复范围只在实验文件，不改正式 gate。
