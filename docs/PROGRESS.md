# RunnerDeck 进度

项目状态的唯一来源。接续工作先读本文件，再读 docs/PLAN.md。

## 路线

- [ ] M0 技术验证 + 工程地基（spike 脚本、stdout 样本、停止方式结论、pyproject/ruff/pytest、ci.yml 首次跑绿）
- [ ] M1 core 层 + CLI（架构守卫、覆盖率门槛）
- [ ] M2 UI 最小可用 + 打包 + release.yml → v0.1.0
- [ ] M3 日常可用（托盘、自启、Poller、导入）→ v0.2.0
- [ ] M4 增强与发布（服务模式、Inno Setup、来源证明）→ v1.0.0

## 当前：M1（代码完成，等 Windows 实机验收）

M0 第 1 轮 spike 已完成；**第 2 轮推迟**（用户决定先做 M1，第 2 轮项目不阻塞 M1，见下方“M0 遗留”）。

M1 已完成：
- [x] 地基：`errors` `models` `paths` `redact` `logs` `winproc`，接口契约 `docs/m1-design.md`（§13 记录实现后的修订）
- [x] `store` `secrets` `github_client` `release` `runner_file` `installer` `process` `status` `manager` `cli`（`runnerdeck-cli` 入口）
- [x] 架构守卫：`core/ruff.toml`（TID251 禁止 Qt/app/ui）+ `tests/core/test_architecture.py`（只有 installer/process 调 cmd.exe，只有 github_client 用 httpx）
- [x] CI：Windows test job 加 core 覆盖率 ≥ 80% 门槛（实际约 99%）
- [x] 对抗评审：6 个维度找问题 + 每条独立反驳，确认 16 条（归并为 7 个问题）全部处理，C14 推迟到 M3
- [ ] **M1 验收（需要 Windows 实机）**：只用 CLI 完成 `login` → `add nablejohne/runnerdeck-sandbox` → `start` + 触发一次 workflow → Ctrl+C 停止 → `remove`；`pytest` 全绿

验收命令（PowerShell，仓库根目录，先 `git pull` 和 `uv sync`）：

```powershell
uv run runnerdeck-cli login                        # 粘贴 fine-grained PAT（Administration RW，选 runnerdeck-sandbox）
uv run runnerdeck-cli add nablejohne/runnerdeck-sandbox --labels runnerdeck-spike
uv run runnerdeck-cli start --all                  # 另开一个终端：gh workflow run spike.yml -R nablejohne/runnerdeck-sandbox -f seconds=20
#   看到 job 完成后按一次 Ctrl+C，应显示优雅停止
uv run runnerdeck-cli status
uv run runnerdeck-cli remove nablejohne/runnerdeck-sandbox -y
uv run pytest
```

验证：`uv run ruff check . && uv run ruff format --check . && uv run pytest`（本地 macOS 1600+ passed）；GitHub CI（ubuntu lint + windows test）绿。

### M0 遗留（第 2 轮 spike，见 spike-notes §9）

不阻塞 M1，但 M2 开始前要跑完：GUI 场景的停止方案（`ctrl_break_helper`）、执行中停止的 job 结论、`.env` 执行策略修复、自更新时的进程与输出、DELETE 兜底、`.runner` 原文、api fixtures；以及 M1 引入的待实测项（§9 末尾）。

## 已完成

### 2026-10-23 M1 core 层 + CLI（代码）

- 6 个模块并行实现（各自带测试）→ manager → cli；整合时修了 manager 读取成功标记的类型错误
- Windows CI 首次跑真实进程测试：假 runner 启动/Ctrl+Break 优雅停止/强杀/崩溃识别、真实 cmd.exe 调用 config.cmd 全部通过；CI 进程有控制台
- 评审确认并修复：Listener 崩溃被当成正常停止（run.cmd 总是 exit 0）、PAT 经环境变量泄漏给 workflow job、404“无权限”被当成“已删除”、config.cmd 不走代理、只信任 certifi、`start` 的 Ctrl+C 处理、runner 目录 ACL 过宽；附带：cmd.exe 绝对路径、解压中断清理、CLI 独立日志文件、`remove --local-only`


### 2026-10-22 项目初始化

- 按 PLAN.md 建好目录（空目录用 `.gitkeep` 占位）
- PLAN.md 补充“CI 与发布流程”一节，并把 CI/发布任务分摊到 M0–M4
- 创建公开仓库 https://github.com/nablejohne/RunnerDeck，许可证 GPLv3

## 决策与坑

- M1 新增 core 模块 `errors/redact/logs/winproc/status/manager`（理由见 m1-design §0）；Controller（M2）只负责把 manager 的调用放进工作线程
- 真实 run.cmd 在 Listener 崩溃后仍 `exit /b 0`，异常只能从 run-helper 打印的 Listener 退出码判断；测试用的假 run.cmd 必须照搬这一点
- GitHub 对“runner 不存在”和“PAT 看不到仓库”都返回 404，只有 list_runners 成功后 404 才算“已删除”
- 传给 config.cmd / run.cmd 的环境一律去掉 PAT 类变量（`winproc.child_environment`）
- TLS 用 truststore 走系统证书库；CLI 和托盘应用写不同的日志文件
- `astral-sh/setup-uv` 没有浮动主版本 tag；Windows 上 TextIOWrapper 默认写 `\r\n`

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
