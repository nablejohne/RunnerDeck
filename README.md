# RunnerDeck

Windows 桌面应用：用一个 PAT 把本机注册成多个个人仓库的 GitHub Actions self-hosted runner，并统一启停、查看状态和日志。

方案见 [docs/PLAN.md](docs/PLAN.md)，进度见 [docs/PROGRESS.md](docs/PROGRESS.md)。

## 下载与安装

到 [Releases](https://github.com/nablejohne/RunnerDeck/releases) 下载其中一种：

- **安装包** `RunnerDeck-<版本>-setup.exe`（推荐）：装到 `%LOCALAPPDATA%\Programs\RunnerDeck`，不需要管理员权限；开始菜单里有快捷方式，在“设置 → 应用”里可以卸载。升级时直接运行新版安装包（先退出 RunnerDeck）。
- **免安装** `RunnerDeck-<版本>-win-x64.zip`：解压到任意目录（不要放在需要管理员权限的位置），双击 `RunnerDeck.exe`。同目录下的 `runnerdeck-helper.exe`（优雅停止 runner 用，必需）和 `runnerdeck-cli.exe`（命令行、服务模式的管理员操作用）不要删。

首次启动会弹出登录框，粘贴你的 PAT（见下文权限）。

**卸载会保留什么**：设置（`%APPDATA%\RunnerDeck`）、runner 目录、凭据管理器里的 PAT 都保留；以 Windows 服务运行的 runner 不依赖 RunnerDeck，卸载后仍在运行。要彻底清理，先在 RunnerDeck 里“移除”不需要的 runner 再卸载。

**SmartScreen 提示“未知发布者”属正常**：程序没有代码签名证书，点“更多信息 → 仍要运行”即可。运行前可以校验文件：

```powershell
Get-FileHash .\RunnerDeck-<版本>-setup.exe -Algorithm SHA256   # 与 Release 里的 SHA256SUMS.txt 对照
```

```bash
gh attestation verify RunnerDeck-<版本>-setup.exe -R nablejohne/RunnerDeck   # 构建来源证明：确认文件由本仓库的 GitHub Actions 构建
```

## PAT 权限

- **Fine-grained token**（推荐）：Repository access 选中要注册 runner 的仓库；Repository permissions 里 **Administration: Read and write**（必需）。想在详情页看 workflow 标签匹配和最近运行，再加 **Contents: Read**、**Actions: Read**（可选）。
- **Classic token**：勾选 `repo`。

PAT 只保存在 Windows 凭据管理器，不写入文件和日志。

## 进程模式与服务模式

- **进程**（默认）：RunnerDeck 启动 runner，退出 RunnerDeck 时优雅停止它们；job 以你的账号运行，能用你的用户目录、PATH 和系统代理。
- **Windows 服务**：添加时勾选“以 Windows 服务运行”，或对已停止的 runner 用“转为服务…”（会重新注册，GitHub 上的 runner id 会变）。需要一次管理员授权（UAC），之后启停不再弹窗。服务以 `NT AUTHORITY\NETWORK SERVICE` 运行：开机即运行、不需要登录，退出或卸载 RunnerDeck 都不影响它。注意：
  - job 看不到你的用户目录，也拿不到用户级 PATH（例如装在用户目录里的 Python、uv）；需要的工具请装到系统级位置。
  - 服务不使用你的系统代理：设置里的代理会写进 runner 目录的 `.env`，对两种模式都生效。
  - runner 根目录不能在用户目录里（NETWORK SERVICE 无法列出它的上级目录，服务会启动失败）。
  - 导入的服务 runner（别处装的）第一次启停前可以用“授权启停…”授权一次。

## 功能概览

- 登录一次，列出你的仓库，几步向导把本机注册成某个仓库的 runner（自动下载并校验 actions/runner）。
- 概览页为每个 runner 显示状态（本地进程 + GitHub 端，自动轮询）和当前 job，可单个或全部启停、打开目录、移除；点卡片进详情页看实时输出和诊断日志。
- 常驻托盘、开机自启、随 RunnerDeck 自动启动 runner、job 失败通知。
- 导入已有的 runner 目录；发现不归 RunnerDeck 管的 runner 进程时可以结束或接管。
- 详情页的 Workflow 页签：仓库里哪些 job 会用到这台 runner，标签不匹配（job 会一直排队）时直接指出；最近运行记录。
- 设置页：runner 根目录（可一键加固权限）、默认标签、代理、轮询间隔、启动与托盘、通知、主题、检查更新、更换 PAT / 退出登录。

## 托盘

RunnerDeck 常驻任务栏右下角的托盘。图标右下角的小圆点表示整体状态：灰色全部停止，绿色在线空闲，蓝色正在执行 job，红色有 runner 出错或需要处理。

- 左键单击托盘图标打开主窗口；右键菜单可以全部启动 / 全部停止、单独启停每个 runner、退出。
- 关闭窗口默认只是隐藏到托盘，runner 继续运行（设置 →“关闭窗口时最小化到托盘”可关掉）。
- 托盘菜单的“退出”才真正退出：先确认（会列出正在执行的 job），再优雅停止所有 runner。
- 再次双击 `RunnerDeck.exe` 不会开第二个实例，而是把已有窗口切到前面。
- Windows 注销或关机时会通知所有 runner 停止，不会卡住注销。

## 开机自启

设置 → 启动与托盘 →“开机自启”。打开后 RunnerDeck 写入当前用户的启动项（`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员权限），登录后以 `RunnerDeck.exe --autostart` 启动：只在托盘，不弹窗口。

- 想让某个 runner 开机后自动上线，在添加向导里勾选“随 RunnerDeck 自动启动”，或在卡片菜单 / 详情页打开“随 RunnerDeck 启动”。RunnerDeck 启动时会拉起这些 runner；如果该目录已经有别的进程在跑，会跳过并提示。
- 挪动了解压目录也没关系：每次启动都会检查启动项，路径不对就改写。
- 如果在任务管理器的“启动”页把 RunnerDeck 禁用了，设置页会提示，请回任务管理器启用（RunnerDeck 不会替你改回来）。
- 平时手动启动也想只在托盘里，打开“启动时最小化到托盘”。

## 导入已有的 runner

已经用官方脚本配置好的 runner 目录（含 `.runner` 文件）可以直接交给 RunnerDeck 管理：概览页“导入”→ 选择目录。导入只登记，不重新注册、不移动目录；PAT 需要能访问这个 runner 注册的仓库。

- RunnerDeck 会扫描 runner 根目录：发现未登记的 runner 目录、或者不是 RunnerDeck 启动的 runner 进程，概览页顶部会出现横幅，可以逐个导入或结束进程。
- 卡片菜单里的“取消登记”只删除 RunnerDeck 的记录，目录和 GitHub 上的注册原样保留（和“移除”不同，“移除”会注销并删除目录）。

## 接管其他进程运行的 runner

如果一个已登记的 runner 正被别的进程运行（比如在终端里用 `runnerdeck-cli start` 启动的），卡片会显示“被其他进程运行”，主按钮变成“接管”：RunnerDeck 结束那个进程，再自己启动它。强制结束后 GitHub 端的旧会话大约 3 分钟才过期，这段时间会显示“等待旧会话过期”，之后恢复空闲；如果当时正在执行 job，强杀会在 GitHub 上留下一个僵尸 job，确认框会提醒。

## 通知

job 结束时托盘弹通知，默认“仅失败”（失败或被放弃的 job），可在设置里改成“全部”或“关闭”。runner 进入出错状态时也会通知；窗口隐藏在托盘时，新发现未登记的 runner 目录或残留进程也会提示。

## 从源码运行

需要 Windows、Python 3.11 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync
uv run python -m runnerdeck          # 图形界面（加 --autostart 只在托盘启动）
uv run runnerdeck-cli --help         # 调试 CLI
uv run pytest                        # 测试（macOS/Linux 上 Windows 专属用例自动跳过）
pwsh scripts/build.ps1               # 打包到 dist\RunnerDeck 和 dist\*.zip
```

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。
