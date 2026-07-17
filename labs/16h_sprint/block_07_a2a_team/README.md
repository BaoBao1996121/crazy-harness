# Block 7: A2A 动态团队与受控自治

一句话结论：Coordinator 动态决定全局 Assignment；普通 Agent 只在当前 Contract 内做有预算的一跳对账，不能自行规划第二跳链路。

## 运行

```powershell
python labs\16h_sprint\block_07_a2a_team\run_demo.py
python -m pytest -q labs\16h_sprint\block_07_a2a_team\fault_check.py
```

运行结果会经过 Scout、Builder、Reviewer，消息进入持久邮箱并由 CooperativeScheduler 唤醒实例。

## 代码地图

- `core/a2a/coordinator.py`: 根据 AgentCard 动态委派。
- `core/a2a/policy.py`: intent/scope/depth/budget/permission 一跳策略。
- `core/a2a/review.py`: Reviewer 只接 EvidencePack。
- `worlds/cicd/team.py`: 可运行团队主路径。

## 准出

- 解释为何既是 `Coordinator -> Agent -> Coordinator`，又允许受控 peer request。
- 说明 A2A envelope 为什么传摘要、引用与契约，而不是共享 full context。
- 能写出越权时交回 Coordinator replan 的条件。
