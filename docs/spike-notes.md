# M0 Spike 结论

M1 实现以本文为准；没有记录的 runner 行为先补 spike，不要猜。“待测”项需要在 Windows 实机跑完后填写。

## 0. 测试环境

- 测试仓库：`nablejohne/runnerdeck-sandbox`（私有），只有一个手动触发的 `spike.yml`，`runs-on: [self-hosted, Windows, runnerdeck-spike]`
- runner 目录：`D:\runners\spike`（脚本默认值，可用 `--dir` 改）
- Windows 版本 / Python 版本 / 是否走代理：待填

## 1. 在 Windows 上怎么跑

```powershell
git clone https://github.com/nablejohne/RunnerDeck.git; cd RunnerDeck
uv sync
$env:RUNNERDECK_TEST_PAT  = "<fine-grained PAT：只选 runnerdeck-sandbox；Administration RW、Actions RW、Contents R>"
$env:RUNNERDECK_TEST_REPO = "nablejohne/runnerdeck-sandbox"

# 1) API（Windows 上再跑一遍，顺便验证 fine-grained PAT 的权限和代理）
uv run python scripts/spike_api.py --save-fixtures

# 2) 安装 + 注册
uv run python scripts/spike_runner.py install

# 3) 采样：空闲 → 成功 job → 失败 job（输出在 spike-out/run-*.log 和 .raw）
uv run python scripts/spike_runner.py run --dispatch-seconds 30          # 看到 job 完成后 Ctrl+C
uv run python scripts/spike_runner.py run --dispatch-seconds 10 --dispatch-fail

# 4) 停止方式对比（每条追加到 spike-out/stop-results.jsonl）
foreach ($m in "ctrl_break","ctrl_break_hidden","kill_listener","taskkill","taskkill_soft") {
  uv run python scripts/spike_runner.py stop-test --method $m
}
uv run python scripts/spike_runner.py stop-test --method ctrl_break --during-job --wait-conclusion 900
uv run python scripts/spike_runner.py stop-test --method taskkill   --during-job --wait-conclusion 900

# 5) 自更新样本：装旧版本，启动后应自动升级
uv run python scripts/spike_runner.py remove --delete-dir
uv run python scripts/spike_runner.py install --version 2.337.0
uv run python scripts/spike_runner.py run                                  # 等升级完成并再次 Listening 后 Ctrl+C

# 6) 移除（两条路径都要测）
uv run python scripts/spike_runner.py remove                               # config.cmd remove
uv run python scripts/spike_runner.py install
uv run python scripts/spike_runner.py remove --api-only --delete-dir       # DELETE /actions/runners/{id}
```

跑完把 `spike-out/` 整个目录打包发回（日志已脱敏，但仍请先扫一眼），我来整理样本进 `tests/fixtures/` 并填写下面各节。

## 2. GitHub REST API

已在 macOS 上用 classic token（`repo, workflow` scope）验证，2026-10-22：

| 调用 | 结果 | 备注 |
| --- | --- | --- |
| `GET /user` | ✅ 0.6s | classic token 有 `X-OAuth-Scopes` 头；fine-grained 没有该头，用来区分 token 类型 |
| `GET /user/repos?affiliation=owner` | ✅ | 只有 12 个仓库，**分页的第二页没测到**；按 `Link: rel="next"` 实现，M1 用 respx 单测覆盖 |
| `POST .../registration-token` | ✅ 3.3s | token 长 29；`expires_at` 带本地时区偏移（`+08:00`），不是 `Z`，解析必须支持时区偏移 |
| `POST .../remove-token` | ✅ | 同上 |
| `GET .../actions/runners` | ✅ | 无 runner 时 `total_count=0` |
| `GET /repos/actions/runner/releases/latest` | ✅ | v2.338.0；**两个 SHA256 来源**：release 正文 `<!-- BEGIN SHA win-x64 -->` 标记，以及 asset 的 `digest` 字段（`sha256:...`），两者一致。M1 优先用 `digest`，正文作兜底 |
| fine-grained PAT 权限是否够 | 待测 | 尤其 `workflow_dispatch` 需要 Actions: Read and write（spike 用，产品本身不需要） |
| 代理（`HTTPS_PROXY`） | 待测 | httpx 默认 `trust_env` 会读取 |

## 3. 注册（config.cmd --unattended）

待测：耗时、完整输出（`spike-out/config-*.log`）、`.runner` 的全部字段、`--replace` 对同名 runner 的行为、输出编码。

## 4. stdout 状态关键字

待测：真实的空闲 / 开始 job / job 完成（成功、失败）/ 自更新行，以及输出是 UTF-8 还是控制台代码页（对比 `.raw` 和 `.log`）。样本放 `tests/fixtures/stdout/`，替换 `spike_common.classify_line` 中的临时正则。

## 5. 停止方式对比

| 方式 | 控制台 | 进程退出耗时 | 残留进程 | 出现 “终止批处理(Y/N)” | GitHub 变 offline 耗时 | job 结论（执行中停止） |
| --- | --- | --- | --- | --- | --- | --- |
| `ctrl_break` | 共享 | | | | | |
| `ctrl_break_hidden` | CREATE_NO_WINDOW | | | | | |
| `kill_listener` | 隐藏 | | | | | |
| `taskkill /T /F` | 隐藏 | | | | | |
| `taskkill /T` | 隐藏 | | | | | |

需要重点回答：托盘应用（pythonw，没有控制台）给 `CREATE_NO_WINDOW` 子进程发 Ctrl+Break 能不能生效？不能的话，M1 选哪种方式（例如在 `run.cmd` 外再套一层自带控制台的 helper，或者直接结束 Listener 再等 GitHub 超时）。

## 6. 自更新

待测：自更新时的 stdout 序列、Listener pid 是否变化、`run.cmd` 进程是否全程存活、更新耗时。

## 7. 移除

待测：`config.cmd remove` 的输出与耗时；`DELETE` 路径后本地目录状态；runner 正在运行时 remove 的行为。

## 8. 对 M1 的结论

待 3–7 节完成后汇总。
