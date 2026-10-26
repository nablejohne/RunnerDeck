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
- **凭据管理器**：SSH（密钥登录）会话里 keyring 报 Win32 1312（“指定的登录会话不存在”），桌面会话应正常；CLI 已提示改用 `RUNNERDECK_PAT`。桌面会话下 `login` / `whoami` / `logout` 已验证通过（2026-10-23）。传统控制台（powershell.exe 窗口）里 Ctrl+V 对 getpass 只产生 U+0016、不会粘贴，已由 `secret_input` 处理。
- **关闭终端会结束前台 runner**（控制台关闭事件发给所有进程），算一次强杀；托盘应用不受影响。
- 目录 ACL：`harden_runner_dir` 生效（只剩 Administrators、SYSTEM、当前用户），runner 注册、运行、移除都正常；`D:\runners` 根目录仍继承 Authenticated Users: 修改（已知缺口）。

## 11. M3 spike（2026-10-24，`ssh XM`，runner 2.338.1）

脚本只在 XM 上临时运行（`D:\tmp\spike_m3.py`），用 M2 注册的 `runnerdeck-sandbox` runner。

- **`_diag` 运行中可读**：Listener 空闲时 `diag.list_diag_logs` / `read_tail(4 KB)` / `read_tail(1 MB)` / 普通 `open(..., "rb")` 都成功，Listener 不会独占日志文件。
- **正在写的日志，`os.scandir` 给的元数据是旧的**：列表里显示 0 B、修改时间停在创建时刻（实际已有 11 KB）。Windows 的目录项缓存要等句柄刷新 / 关闭才更新；`os.stat(path)` 打开文件取到的是实时值。`diag.list_diag_logs` 已改用 `os.stat`，复测显示 12473 B 和当前时间。
- **CLI 从另一个进程 `stop`（强杀）后 `status` 显示“GitHub 会话未过期”**：+13 s / +36 s / +58 s 三次 `status` 都是 SESSION_LINGERING，剩余秒数 238 → 215 → 193，GitHub 端一直在线。M1 遗留的“强杀信息只在内存里”缺口确认已由 `stop_marks` 补上。被强杀的那个进程在原 RunnerProcess 里记为 ERROR（不是我们主动停的），符合预期。
- 强杀约 1 分钟后重新启动：显示 RECONNECTING（等待旧会话过期），Ctrl+Break 优雅停止正常（`graceful=True`）。
- 真机 `pytest`：2328 passed、12 skipped（SSH 会话有控制台，真实 helper / Ctrl+Break 用例都执行了）。**坑**：cmd 里 `set X=v & ...` 会把 `&` 前的空格写进值（`QT_QPA_PLATFORM="offscreen "` -> Qt 找不到插件、进程静默退出、输出为空），要写成 `set "X=v"`。


## 12. M4 服务模式 spike（2026-10-24，`ssh XM`，runner 2.338.1）

脚本只在 XM 上临时运行（`D:\tmp\spike_m4.py`，pywin32），用 sandbox 仓库临时注册 `XM-svc1`（id 29）、`XM-proc1`（id 30 → 转服务后 31），测完全部移除（服务、本地组、目录、GitHub 注册都没有残留）。sandbox 的 `spike.yml` 加了 `label`（默认 `runnerdeck-spike`）和 `probe` 两个输入，让 job 只派给指定标签的 runner。**SSH 会话是管理员高完整性令牌**（`S-1-16-12288`，`TokenElevation=1`），需要管理员的命令都直接在 SSH 里跑；UAC 弹窗本身只能在桌面测（m4-design D1）。

### 安装（`config.cmd --unattended ... --runasservice`）

- 约 7 s，rc 0。输出在 `√ Settings Saved.` 之后多出：`Granting file permissions to 'NT AUTHORITY\NETWORK SERVICE'.`、`Service actions.runner.{owner}-{repo}.{name} successfully installed` / `set recovery option` / `set to delayed auto start` / `configured`、`Waiting for service to start...`、`... started successfully`。**装完立即启动。**
- 服务名 `actions.runner.nablejohne-runnerdeck-sandbox.XM-svc1`，写在 runner 目录的 `.service`（纯文本一行）；显示名 `GitHub Actions Runner (nablejohne-runnerdeck-sandbox.XM-svc1)`；可执行文件 `"<dir>\bin\RunnerService.exe"`；启动类型自动（延迟启动）+ 失败重启；账号 `NT AUTHORITY\NETWORK SERVICE`。
- config.cmd 新建本地组 `GITHUB_ActionsRunner_G<6 位十六进制>`（成员 NETWORK SERVICE，每个服务一个），并给它 runner 目录 `(OI)(CI)(F)`；我们加固过的 DACL 其余部分不变。不生成 `.path`；不改 `.env`（之前写好的 `PSExecutionPolicyPreference` 原样保留）。
- 已注册的目录再跑 `--runasservice`：`Cannot configure the runner because it is already configured...`，rc 1，0.7 s。**进程模式转服务 = `config.cmd remove` 后再 `--runasservice`**：可行，`.env`、`_diag` 保留，agentId 变了（30 → 31）。
- 非管理员令牌下跑 `--runasservice` 的报错**没测到**：提权 SSH 会话里 `runas /trustlevel:0x20000` 仍给高完整性令牌；用 `CreateRestrictedToken`（Administrators 仅拒绝 + Medium）起子进程在 SSH 里 `0xC0000142`（拿不到窗口站）。产品永远经 UAC 跑这一步，影响不大。

### 根目录 ACL：**Listener 要能列出每一级上级目录**

第一次装在只给 Administrators / SYSTEM / 当前用户的根目录 `D:\tmp\m4spike` 下（与 `harden_runner_dir` 相同的 DACL）：服务启动后立刻退出（服务 STOPPED，`Win32ExitCode=1`），Runner 日志：

```text
[... ERR  Terminal] WRITE ERROR: An error occurred: Permission to read the directory contents is required for 'D:\tmp\m4spike\XM-svc1' and each directory up the hierarchy. Access to the path 'D:\tmp\m4spike' is denied.
```

事件日志随后是 `Runner listener exit with terminated error, stop the service, no retry needed.`。给根目录加一条**只作用于本层**的 `Authenticated Users:(RX)` 后，服务正常。结论：根目录加固要保留列目录权限；runner 放在当前用户的主目录下（`C:\Users\<me>` 不让 NETWORK SERVICE 列）时服务模式必然失败。

### 启停（SCM，`win32service`）

| 场景 | 结果 |
| --- | --- |
| 空闲时 STOP | 1.0 s STOPPED；日志 `Exiting...`（优雅）；GitHub 约 36 s 后 offline |
| 停止后立即 START | 0.2 s RUNNING；约 10 s 后 online、`Listening for Jobs`；没有 Conflict |
| 执行 job 时 STOP | 5.4 s STOPPED；`Job spike completed with result: Canceled`；run 结论 failure |
| 首次启动、不配代理 | 约 70 s 才 online（`GetConnectionData` 重试了一分多钟）；XM 能直连 GitHub |

### 授权启停

- 服务默认 DACL：`(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLCSWLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)`：交互用户只能查询；**网络登录（如 SSH）的非管理员连查询都被拒**（5）。
- 追加 `(A;;LCRPWPLORC;;;<用户 SID>)`（`SetServiceObjectSecurity`）后，非管理员令牌（同一用户、Administrators 仅拒绝、Medium，`ImpersonateLoggedOnUser` 模拟）能启动 / 停止 / 查询状态。产品授权时再加 `CC`（查询配置），网络登录下也能读启动类型和账号。
- 非管理员测试方法：在本进程里 `ImpersonateLoggedOnUser(受限令牌)`，SCM 和文件系统的访问检查都按线程令牌做；不要用它测要起子进程的东西。

### 服务模式的日志

- `_diag\Runner_<utc>.log` 每次 Listener 启动一个新文件（属主 NETWORK SERVICE，继承 ACL，当前用户可读）；`Worker_*.log` 同理。
- **stdout 原样进 Runner 日志**：`[2026-10-24 06:08:11Z INFO Terminal] WRITE LINE: 2026-10-24 06:08:11Z: Running job: spike`，`... WRITE LINE: ...: Job spike completed with result: Succeeded`，`... WRITE LINE: Exiting...`；错误是 `[... ERR  Terminal] WRITE ERROR: ...`。去掉 `[...] WRITE LINE: ` 前缀后就是进程模式的 stdout 行，§4 的关键字原样适用。
- Windows 事件日志（Application，来源 `ActionsRunnerService`，事件 ID 100）也有同样的行，外加 `Starting Actions Runner Service` 和服务启停；ID 0 是“服务已成功启动 / 停止”（本地化文字）。
- 版本：`Listener] Version: 2.338.1`。

### job 以 NETWORK SERVICE 运行

`whoami` = `nt authority\network service`，`USERPROFILE=C:\WINDOWS\ServiceProfiles\NetworkService`；PATH 是机器级 PATH（里面指向 `C:\Users\RCHC\...` 的条目读不了：`where python` 找不到 Python39，`git`（Program Files）正常）。`.env` 的 `PSExecutionPolicyPreference=RemoteSigned` 生效（`policy=RemoteSigned`）。

### 代理

`.env` 写 `https_proxy` / `http_proxy`（v2rayN `127.0.0.1:10808`）后重启服务：Runner 日志 `Configuring anonymous proxy http://127.0.0.1:10808 for all HTTPS requests.`，job 里能看到两个变量，`curl api.github.com` 经代理 0.38 s。XM 本身能直连，所以没能证明“不写就连不上”；服务账号不读当前用户的系统代理（WinINET 是按用户的）。

### 移除

管理员 `config.cmd remove --token`（服务在运行）：`Removing service` → `Waiting for service to stop...` → `√ Runner service removed` → `√ Runner removed successfully`，约 5–7 s；服务（1060）和本地组都删掉了。之后**非管理员令牌能删掉整个目录**（`_work` / `_diag` 里 NETWORK SERVICE 建的文件继承了目录 ACL）。

### runner 版本

`win32api.GetFileVersionInfo(<dir>\bin\Runner.Listener.exe)`：FileVersion `2.338.1.0`，ProductVersion `2.338.1+e78522a9…`；`Runner.Listener.exe --version` 输出 `2.338.1`。目录里只有 `bin`、`externals`（没有自更新留下的 `bin.<ver>`）。

### 补充（M4 桌面验收）

- **服务名过长会被截断**：`LAPTOP-8LU6A21K-runnerdeck-sandbox2` 转服务后服务名是 `actions.runner.nablejohne-runnerdeck-sandbox2.LAPTOP-8LU6A21K-runnerdeck-sa-5752`（截断并加编号），显示名仍是完整的。服务名一律从 `.service` 读，不要自己拼。
- **取消注销（D2，Windows 11 26200，结论：程序内修不了）**：开始注销时 RunnerDeck 收到 `WM_QUERYENDSESSION`（`commitDataRequest`，23:09:18 给进程 runner 发了停止信号），随后**被 Windows 直接结束**——没有 `aboutToQuit`、没有 `WM_ENDSESSION(FALSE)`，“正在关闭 2 个应用并注销”界面只列出阻止注销的窗口和一个资源管理器窗口；Tailscale、v2rayN 同样被关掉（XM 的 SSH 随即断开）。点“取消”时 RunnerDeck 早已不在，`app/session_end.py` 的恢复逻辑没有机会运行。M3 记录的“取消注销后不恢复”根因也是这个。处理：README / CHANGELOG 记为已知限制，建议需要扛住注销的 runner 用服务模式；恢复代码保留（无害，系统真的发 `WM_ENDSESSION(FALSE)` 时生效）。

## 13. job 步骤与日志 spike（2026-10-26，runnerdeck-sandbox `spike.yml`，runner 2.338.1）

在开发机轮询 GitHub API，XM 上的进程 runner（GitHub id 28）跑 `seconds=40 fail=true` 和 `seconds=30` 两个 job：

- `GET /repos/{o}/{r}/actions/runs/{run}/jobs`：job 一开始（`in_progress`）就带 `runner_id` / `runner_name`；步骤状态是实时的（约 5 s 内看到 `in_progress` 的步骤），pending 步骤没有 `started_at`
- `GET /actions/jobs/{id}/logs`：运行中也回 302，但跳转到的日志 blob 是 **404**；job 完成后立即可下载（纯文本，带 BOM）。所以运行中拿不到输出，只能看步骤状态
- 日志格式：每行 `2026-10-26T03:29:45.7028124Z <text>`，带 ANSI 颜色；标记 `##[group]Run …` / `##[endgroup]` / `##[error]` / `##[warning]`；跳过的步骤不出现
- 步骤的 `started_at` / `completed_at` 只到秒，相邻步骤常在同一秒：**不能**按时间戳把日志切到步骤 → 失败摘要取“第一条 `##[error]` 之前的 30 行”
- 11 分钟的 AudioTestSystem3.0 构建日志只有 66 KB，整份下载没问题（上限 8 MB，超出保留末尾）
- 找某台 runner 的 job：没有按 runner 查 job 的接口，只能列最新的运行（`runs?per_page=10`）再逐个看 jobs，按 `runner_id` 匹配；已知 job id 时 `GET /actions/jobs/{id}` 一次请求

