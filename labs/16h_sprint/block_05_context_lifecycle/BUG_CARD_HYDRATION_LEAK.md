# Bug Card: 全文召回后永久污染 Context

## 现象

模型按 ref 读取一次大日志后，后续每轮仍携带全文，Context 持续膨胀。

## 不变量

全文召回应有 hydration lease；租期结束后，原文仍在 ArtifactStore，但 active view 回到引用占位。

## 诊断提示

对比相邻两轮的 ContextManifest：同一 source ref 的 representation 应从 `inline` 回到 `ref`。
