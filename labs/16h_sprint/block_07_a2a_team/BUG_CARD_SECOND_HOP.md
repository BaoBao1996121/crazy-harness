# Bug Card: 普通 Agent 规划第二跳

## 现象

Builder 获得 Scout 回复后，又自行请求第三个 Agent，Coordinator 不知情。

## 不变量

普通 Agent 的自治受 intent 白名单、scope、permission、peer budget 和 `max_depth=1` 共同限制；越界必须升级给 Coordinator。

## 诊断提示

不要只看 receiver 是否存在。逐项打印 Contract 与 Request 的 depth、scope、permissions、budget_cost 和 intent。
