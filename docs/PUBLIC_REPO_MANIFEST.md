# Public Repository Manifest

## 可以公开

- `crazy_harness/` 中自主实现的运行时代码
- `frontend/` 源码，不包含 `node_modules` 与构建产物
- `tests/`、可复现的 `examples/` 和 Golden Task fixtures
- 通用架构文档、ADR、公开来源链接和学习教程
- `.env.example`，不得包含真实 Key

## 默认不公开

- `runs/`、`output/`、`outputs/`、SQLite 数据库和浏览器截图
- `research_sources/` 上游仓库镜像
- 飞书原文、Obsidian Vault、内部附件和个人聊天内容
- 含本机绝对路径、用户名、Token、Cookie、SSH Key 或云账号的信息
- 未完成许可证审查的第三方源码、Prompt、Skill 和测试夹具

## 发布前检查

```text
secret scan
→ absolute-path scan
→ license/SBOM check
→ clean-clone install
→ backend/frontend tests
→ Docker quickstart
→ examples smoke
→ docs link check
```

Public CI 通过不代表可以自动发布；首个公开版本必须人工审阅实际 Git diff 和仓库历史。
