# RunnerDeck

Windows 托盘应用：管理多个仓库级 GitHub Actions self-hosted runner（进程模式或 Windows 服务）。完整方案见 docs/PLAN.md，runner 实测结论见 docs/spike-notes.md，core 层接口见 docs/m1-design.md，M2 的 app/ui/打包契约见 docs/m2-design.md，M3 / M4 见 docs/m3-design.md、docs/m4-design.md（各自末节记实现后的修订），进度见 docs/PROGRESS.md。

## 技术栈
- Python 3.11, PyQt5 5.15, PyQt-Fluent-Widgets (import qfluentwidgets)
- httpx, pydantic v2, keyring, psutil, pywin32（只在 Windows）, PyYAML
- uv 管理依赖，ruff 格式化与检查，pytest + respx 测试

## 常用命令
- 安装依赖：uv sync
- 运行：uv run python -m runnerdeck（`--version` 打印版本；`--autostart` 只在托盘启动、不显示窗口，开机自启写进 HKCU Run 键的就是它；`--smoke-test` 离屏建窗口和托盘、切换各页面（含详情页）后退出，打包产物自检用）
- 调试 CLI：uv run runnerdeck-cli --help（或 uv run python -m runnerdeck.core.cli --help）
- 控制台 helper：uv run runnerdeck-helper <runner_dir>（GUI 启动 runner 时自动调用，给 run.cmd 一个隐藏控制台以便 Ctrl+Break 优雅停止；手动运行只用于排障，stdin 输入 `stop` 回车即停）
- 测试：uv run pytest
- 检查：uv run ruff check . ; uv run ruff format --check .
- 打包（Windows）：pwsh scripts/build.ps1 → dist/RunnerDeck-<版本>-win-x64.zip（含 RunnerDeck.exe / runnerdeck-helper.exe / runnerdeck-cli.exe）；装了 Inno Setup 时再出 dist/RunnerDeck-<版本>-setup.exe（packaging/RunnerDeck.iss，按用户安装；`-NoInstaller` 跳过）；图标：uv run python scripts/make_icon.py
- 服务模式的管理员操作：`runnerdeck-cli elevated <请求文件>`（隐藏子命令，由 UAC 拉起，见 core/elevate.py；手动运行只用于排障）
- 发布：改 pyproject 版本 + CHANGELOG → 推 main → `git tag vX.Y.Z && git push origin vX.Y.Z`（详见 docs/PLAN.md“CI 与发布流程”）；Release 产物：zip、setup.exe、SHA256SUMS.txt + 构建来源证明

## 架构规则
- 依赖方向只能是 ui -> app -> core；core 不得 import PyQt5 或 qfluentwidgets
- UI 线程不得做网络或子进程调用；耗时操作放 app/workers.py，结果通过信号返回
- GitHub 调用只能经过 core/github_client.py
- 调用 config.cmd / run.cmd 的代码只能在 core/installer.py、core/process.py 和 core/console_helper.py（GUI 的 Ctrl+Break 中继，只用标准库）；提权进程 core/elevated.py 也只经 installer 调 config.cmd
- 服务控制管理器（win32service / win32security）只在 core/service.py；ShellExecuteExW（UAC）只在 core/elevate.py
- ui 只能通过 app/controller.py 使用 core（可直接 import 的只有 models / status / errors / paths / account / redact 这类纯数据模块）
- config.json 一律原子写（临时文件 + os.replace）

## 安全
- PAT 只存 keyring，绝不写入文件、日志、异常信息或测试快照；日志中的 token 一律脱敏
- 不读写 runner 目录里的 .credentials* 文件
- 集成测试读取 RUNNERDECK_TEST_PAT 和 RUNNERDECK_TEST_REPO，未设置时自动跳过

## 约定
- 界面文案用中文；代码、注释、提交信息用英文
- core 层的新功能必须附单测
- CI 和发布只用 GitHub 托管 runner，本仓库 workflow 不得使用 `runs-on: self-hosted`
- 每个里程碑收尾更新 docs/PROGRESS.md
- 对 runner 行为不确定时，先查 docs/spike-notes.md；没有记录就先写 spike 验证，不要猜
- Windows 上写给重定向 stdout 的脚本（发布说明等）显式输出 UTF-8 字节；CI 的 pwsh 步骤里预期的非零退出码之后清零 `$LASTEXITCODE`
- 只在 Windows 上走到的代码（pywin32、注册表、原生事件、Windows 路径）改完先在 XM 上跑一遍真机 pytest 再推送（同步用完整的 `git archive HEAD` + `COPYFILE_DISABLE=1 tar` 未提交改动）
