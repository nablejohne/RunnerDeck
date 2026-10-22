# RunnerDeck 进度

项目状态的唯一来源。接续工作先读本文件，再读 docs/PLAN.md。

## 路线

- [ ] M0 技术验证 + 工程地基（spike 脚本、stdout 样本、停止方式结论、pyproject/ruff/pytest、ci.yml 首次跑绿）
- [ ] M1 core 层 + CLI（架构守卫、覆盖率门槛）
- [ ] M2 UI 最小可用 + 打包 + release.yml → v0.1.0
- [ ] M3 日常可用（托盘、自启、Poller、导入）→ v0.2.0
- [ ] M4 增强与发布（服务模式、Inno Setup、来源证明）→ v1.0.0

## 当前：M0（进行中）

第 1 轮 Windows 实机 spike 已完成并整理（结论见 docs/spike-notes.md §2–§8），**等第 2 轮**（§9）。

- [x] 工程地基：pyproject / uv.lock / ruff / pytest / CHANGELOG / ci.yml / dependabot
- [x] spike 脚本：`spike_api.py`、`spike_runner.py`、`spike_console_helper.py`（第 2 轮新增）
- [x] 私有测试仓库 `nablejohne/runnerdeck-sandbox`
- [x] 第 1 轮：注册、移除、stdout 真实样本、5 种停止方式 → 样本进 `tests/fixtures/`，`classify_line` 换成真实正则
- [ ] 第 2 轮：执行中停止、helper 方式 Ctrl+Break、`.env` 执行策略修复、自更新、DELETE 兜底、`.runner` 样本、api fixtures
- [ ] 填完 spike-notes §8，M0 收尾（评估是否需要调整 PLAN 的 M1/M2）

验证：`uv run ruff check . && uv run ruff format --check . && uv run pytest`（22 passed）；GitHub CI 绿。

## 已完成

### 2026-10-22 项目初始化

- 按 PLAN.md 建好目录（空目录用 `.gitkeep` 占位）
- PLAN.md 补充“CI 与发布流程”一节，并把 CI/发布任务分摊到 M0–M4
- 创建公开仓库 https://github.com/nablejohne/RunnerDeck，许可证 GPLv3

## 决策与坑

- 只有和 runner 共享控制台的 Ctrl+Break 能干净停止（11 秒 offline、可立即重启）；`CREATE_NO_WINDOW` 下静默无效；强杀会让旧会话残留约 3 分钟
- `stdin=DEVNULL` 是硬性要求，否则 Ctrl+Break 会卡在“终止批处理操作吗(Y/N)?”
- 个人电脑默认执行策略 Restricted，`shell: powershell` 步骤全部失败；候选修复是 runner `.env` 写 `PSExecutionPolicyPreference`
- `config.cmd remove` 会删掉 `.runner`，必须先读出 `agentId`

- M0 spike 辅助函数放在 `scripts/spike_common.py`，pytest 用 `pythonpath = ["scripts"]` 导入；M1 由 core/ 重写后删除这段配置
- 下载 runner zip 用不带 Authorization 的独立 httpx client，避免把 PAT 发给 CDN
- fixture 只保留测试仓库的条目和最少字段，其他私有仓库名不进公开仓库
- 已发现：`registration-token` 的 `expires_at` 带 `+08:00` 偏移；release asset 自带 `digest` 字段可直接做 SHA256 校验
- `astral-sh/setup-uv` 没有浮动主版本 tag，必须写完整版本（首次 CI 因 `@v10` 失败）
- spike 的 `workflow_dispatch` 需要 PAT 有 Actions: Read and write，产品本身只要 Read

- 公开仓库的 CI / 发布只跑 GitHub 托管 runner，不用自托管 runner（fork PR 可在本机执行代码）
