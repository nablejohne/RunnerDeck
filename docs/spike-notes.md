# M0 Spike 结论

M1 实现以本文为准；没有记录的 runner 行为先补 spike，不要猜。

- 第 1 轮：2026-10-22，Windows（中文系统，控制台代码页 936），runner 2.338.0 / 2.337.0，原始输出见 `tests/fixtures/`
- 第 2 轮：2026-10-23，通过 SSH 在同一台 Windows 上跑（runner 2.338.1 / 2.337.0），结果见 §10

## 0. 测试环境

- 测试仓库：`nablejohne/runnerdeck-sandbox`（私有），只有手动触发的 `spike.yml`，`runs-on: [self-hosted, Windows, runnerdeck-spike]`
- runner 目录：`D:\runners\spike`；脚本在 `D:\tmp\RunnerDeck` 下用 uv 运行

## 1. 在 Windows 上怎么跑

```powershell
git clone https://github.com/nablejohne/RunnerDeck.git; cd RunnerDeck   # 已有则 git pull
uv sync
$env:RUNNERDECK_TEST_PAT  = "github_pat_..."   # token 的值，不是名字；只选 runnerdeck-sandbox
$env:RUNNERDECK_TEST_REPO = "nablejohne/runnerdeck-sandbox"
# PAT 权限：Administration RW、Actions RW（spike 自动触发 workflow 用；产品只需 Read）、Contents R

uv run python scripts/spike_api.py --save-fixtures
uv run python scripts/spike_runner.py install
uv run python scripts/spike_runner.py run --dispatch-seconds 30           # job 完成后 Ctrl+C
uv run python scripts/spike_runner.py stop-test --method <方式> [--during-job --wait-conclusion 900]
uv run python scripts/spike_runner.py remove [--api-only] [--delete-dir]
```

## 2. GitHub REST API

| 调用 | 结果 | 备注 |
| --- | --- | --- |
| `GET /user` | ✅ 0.6s | classic token 有 `X-OAuth-Scopes` 头；fine-grained 没有，可用来区分类型 |
| `GET /user/repos?affiliation=owner` | ✅ | 12 个仓库，**第二页没测到**；按 `Link: rel="next"` 实现，M1 用 respx 单测覆盖 |
| `POST .../registration-token` | ✅ 3.3s | token 长 29；`expires_at` 带 `+08:00` 偏移而非 `Z`，解析必须支持时区偏移 |
| `POST .../remove-token` | ✅ | 同上 |
| `GET .../actions/runners` | ✅ | 无 runner 时 `total_count=0`；状态变化见 §5 |
| `GET /repos/actions/runner/releases/latest` | ✅ | **两个 SHA256 来源**：asset 的 `digest`（`sha256:...`）和正文 `<!-- BEGIN SHA win-x64 -->` 标记，一致。M1 用 `digest`，正文兜底 |
| fine-grained PAT | 部分 ✅ | Windows 上 install / remove / workflow 自动触发都成功，说明 Administration、Actions 写权限够用；`spike_api.py` 的输出和 api fixtures 尚未回传 |
| PAT 填错 | — | 把 token 名字当值填会得到 6 个 401；脚本已改为启动时检查前缀（`github_pat_` / `ghp_` 等），**M1 登录页同样要做这个检查** |

## 3. 注册与移除

- `config.cmd --unattended ... --replace`：成功输出三行 `√ Connected to GitHub` / `√ Runner successfully added` / `√ Settings Saved.`（`tests/fixtures/config/config_ok.txt`），前面有一大段 ASCII 横幅，M1 只按 `√` 行判断进度，失败时展示全部输出。
- `config.cmd remove --token`：`√ Runner removed successfully` / `√ Removed .credentials` / `√ Removed .runner`。即**移除后 `.runner` 被删**，M1 必须在调用 remove 之前读出 `agentId`，供兜底的 `DELETE` 使用。
- 待补：`.runner` 完整字段样本、`--api-only`（`DELETE`）路径的结果（第 1 轮两次 remove 都走的 config.cmd）。

## 4. stdout 状态关键字（已用真实输出替换临时正则）

真实序列（`tests/fixtures/stdout/job_then_ctrl_break.txt`）：

```text
已复制         1 个文件。                       ← run.cmd 自己 copy 的输出，GBK
√ Connected to GitHub
Current runner version: '2.338.0'
2026-10-22 08:09:45Z: Listening for Jobs
2026-10-22 08:09:53Z: Running job: spike
2026-10-22 08:10:02Z: Job spike completed with result: Failed
Exiting...                                       ← 收到 Ctrl+Break
终止批处理操作吗(Y/N)?                           ← cmd 的提示，stdin=DEVNULL 时自动结束，GBK
"Runner listener exit with 0 return code, stop the service, no retry needed."
"Exiting runner..."
```

会话冲突时（`session_conflict_reconnect.txt`）：

```text
A session for this runner already exists.
2026-10-22 08:16:53Z: Runner connect error: Error: Conflict. Retrying until reconnected.
（约每 31–33 秒重试一次，每次都输出 √ Connected to GitHub + A session ... already exists.）
2026-10-22 08:19:06Z: Runner reconnected.
```

| 事件 | 匹配 | 说明 |
| --- | --- | --- |
| connected | `Connected to GitHub$` | 注意：会话冲突重试时也会出现，**不代表在线** |
| conflict | `A session for this runner already exists` | 旧会话未过期，见 §5 |
| reconnected | `Runner reconnected.` | |
| version | `Current runner version: '(x.y.z)'` | |
| idle | `Listening for Jobs` | |
| busy | `Running job: (name)` | job 名是 workflow 里的 job id |
| completed | `Job (name) completed with result: (Result)` | 见过 `Failed`；`Succeeded` 待第 2 轮 |
| stopping | `^Exiting...$` | |
| exited | `Runner listener exit with (n) return code` / `Exiting after unknown error code: (n)` | |

**编码**：runner 自己的行是 ASCII/UTF-8；cmd.exe 自己的行（copy 提示、Y/N 提示、taskkill 输出）是控制台代码页（中文系统为 GBK）。**按行解码：先试 UTF-8，失败再用控制台代码页**（`decode_output`）。

## 5. 停止方式对比（不在执行 job 时）

| 方式 | 控制台 | 进程退出 | 残留进程 | Y/N 提示 | GitHub 变 offline | 旧会话占用（之后重启要等） |
| --- | --- | --- | --- | --- | --- | --- |
| `ctrl_break` | 共享 | **0.36s**，rc=0 | 无 | 出现，但 stdin=DEVNULL 自动结束 | **11s** | **无**，立即可重启 |
| `ctrl_break_hidden` | CREATE_NO_WINDOW | **无效**，15s 未退出 | Listener 留着 | — | 强杀后 69s | 约 3 分 10 秒 |
| `kill_listener` | 隐藏 | 0.05s，rc=0 | 无 | — | 69s | 约 3 分 25 秒 |
| `taskkill /T /F` | 隐藏 | 0.34s | 无 | — | 69s | 约 3 分 30 秒 |
| `taskkill /T`（无 /F） | 隐藏 | **无效**：“只能强行终止” | Listener 留着 | — | 强杀后 69s | 约 2 分 45 秒 |

结论：

1. **只有 Ctrl+Break 是干净停止**：Listener 会主动删掉会话，GitHub 11 秒内变 offline，马上能再启动。
2. Ctrl+Break **必须和 runner 共享同一个控制台**；子进程用 `CREATE_NO_WINDOW` 时发过去静默无效（不报错）。托盘应用（pythonw）没有控制台，这个问题 M2 必须解决，候选方案 `ctrl_break_helper` 第 2 轮验证。
3. Y/N 提示不是问题，前提是 **`stdin=DEVNULL`**（M1 硬性要求）。
4. 所有强杀都会让 GitHub 端会话残留约 3–3.5 分钟。这期间重新启动会反复 `Conflict` 重试，runner 能自己恢复，但 UI 要显示“等待旧会话过期”，不能当成故障。GitHub API 的 `status` 约 69 秒就变 offline，比会话过期早得多，**不能用 offline 判断“可以重启了”**。
5. 用 psutil 结束 Listener（退出码 15）时，run-helper 输出 `Exiting after unknown error code: 15` 后不会重启，所以兜底强杀不会被自动拉起。
6. `taskkill /T /F` 的进程树：`cmd.exe(run.cmd)` → `cmd.exe(run-helper)` → `Runner.Listener.exe`，三个都会被结束。

## 6. 执行 job 时停止（第 1 轮无效，第 2 轮结果见 §10）

第 1 轮所有 job 大约 9.5 秒就**自己失败了**，比停止信号还早（`job_settle` 等了 10 秒），所以“执行中停止”的两条数据实际上是在 job 结束后才停的，作废。

原因：测试机 PowerShell 执行策略是默认的 **Restricted**，`shell: powershell` 的步骤第一行就报 `running scripts is disabled on this system`。

- 这是**产品层面的发现**：个人电脑上的 self-hosted runner，凡是 `shell: powershell` 的步骤都会失败（GitHub 托管的 Windows runner 已放开执行策略，所以 workflow 作者不会注意到）。
- 候选修复：在 runner 目录的 `.env` 写 `PSExecutionPolicyPreference=RemoteSigned`，只对该 runner 的子进程生效（Process 作用域），不改系统设置。第 2 轮验证。
- sandbox 的 `spike.yml` 已改：sleep 用 `cmd` + `ping`，另加一个 `continue-on-error` 的 PowerShell 探测步骤。

## 7. 自更新（第 1、2 轮都未触发，见 §10）

装 2.337.0 跑了 8 分钟没有更新。runner 的自更新是**分配 job 时**由服务端下发的，第 1 轮那次没有派 job。第 2 轮用 `run --dispatch-seconds` 再测。

## 8. 对 M1 的结论（目前已确定的）

- 启动：`Popen(["cmd.exe", "/c", "run.cmd"], stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, creationflags=CREATE_NEW_PROCESS_GROUP)`，M1 的 CLI 在控制台里运行，与 runner 共享控制台。
- 停止：先 Ctrl+Break，最多等 15 秒；仍未退出再 `taskkill /PID <cmd> /T /F`，此时把状态标为“等待旧会话过期（约 3 分钟）”。
- 状态：stdout 解析采用 §4 的事件表；`connected` 不代表在线，`conflict` 映射为“重连中”。
- 移除：先读 `.runner` 的 `agentId`，再 `config.cmd remove`；失败时用 `DELETE`。
- 输出解码：按行，UTF-8 优先，控制台代码页兜底。
- 待定（第 2 轮决定）：GUI 下的停止方案（helper）、`.env` 里是否默认写入执行策略、自更新期间的进程跟踪。

## 9. 第 2 轮要跑的（已完成，结果见 §10）

先 `git pull`，然后：

```powershell
# a) 重新安装，同时测试执行策略的 .env 修复
uv run python scripts/spike_runner.py remove --delete-dir
uv run python scripts/spike_runner.py install --env PSExecutionPolicyPreference=RemoteSigned
Get-Content D:\runners\spike\.runner          # 把内容一起发回（不含密钥）

# b) 成功 job 样本 + PowerShell 探测（到 GitHub 上看 "PowerShell probe" 步骤是否成功）
uv run python scripts/spike_runner.py run --dispatch-seconds 20          # 看到 completed 后 Ctrl+C

# c) 托盘场景候选：helper 转发 Ctrl+Break
uv run python scripts/spike_runner.py stop-test --method ctrl_break_helper

# d) 执行 job 时停止（这次 job 会真的跑 180 秒）
uv run python scripts/spike_runner.py stop-test --method ctrl_break        --during-job --wait-conclusion 900
uv run python scripts/spike_runner.py stop-test --method ctrl_break_helper --during-job --wait-conclusion 900
uv run python scripts/spike_runner.py stop-test --method taskkill          --during-job --wait-conclusion 900

# e) 自更新：装旧版本并派一个 job
uv run python scripts/spike_runner.py remove --delete-dir
uv run python scripts/spike_runner.py install --version 2.337.0
uv run python scripts/spike_runner.py run --dispatch-seconds 20          # 等更新完、job 结束、再次 Listening 后 Ctrl+C

# f) DELETE 兜底路径
uv run python scripts/spike_runner.py remove --api-only --delete-dir
```

M1 实现中引入、需要第 2 轮实测确认的（来自 runner 源码，未抓到过原始输出）：

- `config.cmd --replace` 覆盖同名 runner 时是否输出 `Successfully replaced the runner`（installer 已兼容）
- run-helper 对 Listener 退出码 1/2/3/4/5 打印的原句（`terminated error` / `retryable error` / `because of updating` / `re-launch ephemeral` / `Session Conflict error`），process.py 已按源码措辞识别
- 真实的 `Runner.Listener.exe` / `Runner.Worker.exe` 进程能被 `find_runner_processes` 找到，`runnerdeck-cli stop` 能从另一个终端结束它们
- `harden_runner_dir` 收紧 ACL 后 runner 能正常注册、运行、自更新、移除

发回：`spike-out/`（可以删掉 `cache/` 里的两个 zip，约 200 MB）、`tests/fixtures/api/`、`.runner` 内容，以及 a、f 两步的控制台输出（这两步的部分结果只打印在控制台）。

## 10. 第 2 轮结果（2026-10-23）

环境：`ssh XM`（Windows 11 26200，中文系统，系统代理 v2rayN 127.0.0.1:10808，执行策略未设置 = Restricted），fine-grained PAT（只选 runnerdeck-sandbox）。SSH 会话里 `has_console()` 为 True（sshd 为会话创建了控制台），Ctrl+Break 相关测试都真正执行了。

### 停止

| 场景 | 退出耗时 | 结果 |
| --- | --- | --- |
| job 执行中，共享控制台 Ctrl+Break | 15.16 s | 先输出 `Exiting...`，runner 取消 job（`Job spike completed with result: Canceled`），再正常退出；run 的结论是 failure，job 是 Canceled |
| 空闲，`ctrl_break_helper`（helper 自带隐藏控制台转发 Ctrl+Break） | **0.91 s** | 优雅退出、无残留。**托盘应用（无控制台）的停止方案可行**，M2 采用 |
| job 执行中，`ctrl_break_helper` | 15.53 s | 同上，job 被取消 |
| job 执行中，旧 CLI（15 s 超时） | 15 s 后强杀 | job 在 GitHub 上一直卡在 in_progress（> 8 分钟），runner 一直 busy=true，`gh run cancel` 无效，**DELETE 返回 422 “currently running a job and cannot be deleted”**；只有 `POST .../runs/{id}/force-cancel` 能立刻解开 |

结论：job 执行中的优雅停止需要 ≥ 15 s，**process.py 已改为执行 job 时默认等 90 s、空闲 15 s**；真机复测：Ctrl+C 后约 16 s 优雅退出、job Canceled、没有强杀。强杀会让 job 变成僵尸，manager 对 422 给出 force-cancel 的提示。

GitHub API 的 offline 状态在优雅停止后 11–70 s 才更新（有波动），但会话是立即释放的：紧接着重启没有出现 Conflict。强杀后会话约 2–3.5 分钟才过期，期间重启显示“等待旧会话过期”并自动恢复（CLI 已验证）。

### runner 在 GitHub 上被删除

运行中被 `DELETE /actions/runners/{id}` 后，输出（`tests/fixtures/stdout/deregistered_on_github.txt`）：

```text
The runner no longer exists on the server. Cleaning up local configuration.
√ Removed .credentials
√ Removed .runner
"Runner listener exit with 0 return code, stop the service, no retry needed."
```

**不是** run-helper 的 “terminated error”，退出码也是 0。process.py 已把这一行识别为 DEREGISTERED -> ERROR（“请先 remove 再重新 add”）。runner 自己删了 `.runner`，所以之后 `remove` 走 API 删除。

### 注册 / 移除 / API

- `--replace` 覆盖同名 runner：`A runner exists with the same name` + `√ Successfully replaced the runner`，**runner id 不变**（`tests/fixtures/config/config_replaced.txt`）。
- 对已在 GitHub 上不存在的 runner 执行 `config.cmd remove`：`Does not exist. Skipping Removing runner from the server`，随后仍是 `√ Runner removed successfully`，rc=0（`remove_already_gone.txt`）。
- **`DELETE /actions/runners/{id}` 是幂等的**：对不存在的 id 也返回 204（只有 GET 返回 404）。
- `.runner` 实际字段：`agentId, agentName, poolId, poolName, serverUrl, gitHubUrl, workFolder, useV2Flow, serverUrlV2`（`tests/fixtures/runner/runner_file.json`，serverUrl 的租户段已替换）。
- fine-grained PAT：`/user/repos` 只返回 PAT 选中的仓库；6 个 API 调用全部通过（`tests/fixtures/api/`）。

### 其他

- **PowerShell 执行策略**：runner 目录 `.env` 写 `PSExecutionPolicyPreference=RemoteSigned` 后，`shell: powershell` 步骤输出 `policy=RemoteSigned`，修复有效，不改系统设置。M2 添加向导里提供这个选项（默认开）。
- **自更新**：2.337.0 运行 6 分钟、执行过 1 个 job，仍未触发更新；GitHub 不会强制更新只落后一个小版本的 runner，无法按需复现。run-helper 的更新相关输出仍按源码措辞识别，等自然发生时再核对。
- **凭据管理器**：SSH（密钥登录）会话里 keyring 报 Win32 1312（“指定的登录会话不存在”），桌面会话应正常；CLI 已提示改用 `RUNNERDECK_PAT`。桌面会话下的 `login`/`logout` 待用户手动验证一次。
- **关闭终端会结束前台 runner**（控制台关闭事件发给所有进程），算一次强杀；托盘应用不受影响。
- 目录 ACL：`harden_runner_dir` 生效（只剩 Administrators、SYSTEM、当前用户），runner 注册、运行、移除都正常；`D:\runners` 根目录仍继承 Authenticated Users: 修改（已知缺口）。
