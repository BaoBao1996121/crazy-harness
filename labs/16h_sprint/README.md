# Crazy Harness 16 小时引导式调试实验

这里保存与八个学习块对应的可运行场景、Bug Card、伪代码模板和 Trace。

## 使用规则

1. 先运行 known-good，再看故障版本。
2. 注入 Bug 前先写预测，不先看答案。
3. 调试时优先读测试、EventLog 和最后一个可信事件。
4. 修复后关闭 fault injector，完整测试必须重新全绿。
5. 每块结束时闭卷画图并完成伪代码，不要求从空文件实现整个 Python 模块。

## 学习块

| Block | 主题 | 当前状态 |
|---:|---|---|
| 1 | Agent Loop、known-good 与 naive baseline | 可运行 |
| 2 | Phase、Command Validation、Crash Recovery | 可运行 |
| 3 | Contract、Progress、Completion Gate | 可运行 |
| 4 | Resident Runtime、Wait、UNKNOWN | 可运行 |
| 5 | Context、Offloading、Microcompact | 可运行 |
| 6 | Tool Pipeline、Policy、Hooks、Runtime | 可运行 |
| 7 | A2A Teamwork、Peer Policy、Reviewer | 可运行 |
| 8 | Trace、Replay、Eval、Memory、Evolution | 可运行 |

每个 Block 目录都有三类入口：`run_demo.py` 跑正式 known-good；`fault_check.py` 稳定复现一个故意缺陷；`PSEUDOCODE_TEMPLATE.md` 用于脱离 Python 复述机制。
