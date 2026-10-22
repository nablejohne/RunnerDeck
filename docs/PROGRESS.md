# RunnerDeck 进度

项目状态的唯一来源。接续工作先读本文件，再读 docs/PLAN.md。

## 路线

- [ ] M0 技术验证 + 工程地基（spike 脚本、stdout 样本、停止方式结论、pyproject/ruff/pytest、ci.yml 首次跑绿）
- [ ] M1 core 层 + CLI（架构守卫、覆盖率门槛）
- [ ] M2 UI 最小可用 + 打包 + release.yml → v0.1.0
- [ ] M3 日常可用（托盘、自启、Poller、导入）→ v0.2.0
- [ ] M4 增强与发布（服务模式、Inno Setup、来源证明）→ v1.0.0

## 已完成

### 2026-10-22 项目初始化

- 按 PLAN.md 建好目录（空目录用 `.gitkeep` 占位）
- PLAN.md 补充“CI 与发布流程”一节，并把 CI/发布任务分摊到 M0–M4
- 创建公开仓库 https://github.com/nablejohne/RunnerDeck，许可证 GPLv3

## 决策与坑

- 公开仓库的 CI / 发布只跑 GitHub 托管 runner，不用自托管 runner（fork PR 可在本机执行代码）
