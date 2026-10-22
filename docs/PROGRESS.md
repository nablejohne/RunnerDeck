# RunnerDeck 进度

项目状态的唯一来源。接续工作先读本文件，再读 docs/PLAN.md。

## 路线

- [ ] M0 技术验证 + 工程地基（spike 脚本、stdout 样本、停止方式结论、pyproject/ruff/pytest、ci.yml 首次跑绿）
- [ ] M1 core 层 + CLI（架构守卫、覆盖率门槛）
- [ ] M2 UI 最小可用 + 打包 + release.yml → v0.1.0
- [ ] M3 日常可用（托盘、自启、Poller、导入）→ v0.2.0
- [ ] M4 增强与发布（服务模式、Inno Setup、来源证明）→ v1.0.0

## 当前：M0（进行中）

工程地基与 spike 脚本已落地，**等 Windows 实机跑 spike**（步骤见 docs/spike-notes.md §1）。

- [x] `pyproject.toml` + `uv.lock`（Python 3.11，hatchling，src 布局）、ruff、pytest、`CHANGELOG.md`
- [x] `ci.yml`（ubuntu `lint` + windows `test`）、`dependabot.yml`（actions + uv 周更）
- [x] `scripts/spike_common.py`：环境变量、脱敏、API 封装、SHA256 提取、`.runner` 解析、stdout 分类（临时正则）
- [x] `scripts/spike_api.py`：6 个调用在 macOS 上全部通过（classic token）
- [x] `scripts/spike_runner.py`：install / run / stop-test / procs / remove（仅 Windows，**尚未实机运行**）
- [x] 私有测试仓库 `nablejohne/runnerdeck-sandbox`（手动触发的 `spike.yml`）
- [ ] Windows 实机：注册、stdout 样本、5 种停止方式、执行中停止、自更新、两种移除路径
- [ ] 样本进 `tests/fixtures/`，`classify_line` 换成真实正则，填完 spike-notes §3–§8

验证：`uv run ruff check . && uv run ruff format --check . && uv run pytest`（18 passed）；GitHub CI 绿。

## 已完成

### 2026-10-22 项目初始化

- 按 PLAN.md 建好目录（空目录用 `.gitkeep` 占位）
- PLAN.md 补充“CI 与发布流程”一节，并把 CI/发布任务分摊到 M0–M4
- 创建公开仓库 https://github.com/nablejohne/RunnerDeck，许可证 GPLv3

## 决策与坑

- M0 spike 辅助函数放在 `scripts/spike_common.py`，pytest 用 `pythonpath = ["scripts"]` 导入；M1 由 core/ 重写后删除这段配置
- 下载 runner zip 用不带 Authorization 的独立 httpx client，避免把 PAT 发给 CDN
- fixture 只保留测试仓库的条目和最少字段，其他私有仓库名不进公开仓库
- 已发现：`registration-token` 的 `expires_at` 带 `+08:00` 偏移；release asset 自带 `digest` 字段可直接做 SHA256 校验
- spike 的 `workflow_dispatch` 需要 PAT 有 Actions: Read and write，产品本身只要 Read

- 公开仓库的 CI / 发布只跑 GitHub 托管 runner，不用自托管 runner（fork PR 可在本机执行代码）
