# RunnerDeck

Windows 桌面应用：用一个 PAT 把本机注册成多个个人仓库的 GitHub Actions self-hosted runner，并统一启停、查看状态和日志。

> 开发中。方案见 [docs/PLAN.md](docs/PLAN.md)，进度见 [docs/PROGRESS.md](docs/PROGRESS.md)。

## 下载与运行

1. 到 [Releases](https://github.com/nablejohne/RunnerDeck/releases) 下载 `RunnerDeck-<版本>-win-x64.zip`。
2. 解压到任意目录（不要放在需要管理员权限的位置），双击 `RunnerDeck.exe`。
   同目录下的 `runnerdeck-helper.exe`（优雅停止 runner 用，必需）和 `runnerdeck-cli.exe`（排障用）不要删。
3. 首次启动会弹出登录框，粘贴你的 PAT（见下文权限）。

**SmartScreen 提示“未知发布者”属正常**：程序没有代码签名证书。点“更多信息 → 仍要运行”即可。想留存校验值，可在 PowerShell 里计算 zip 的 SHA256（`SHA256SUMS.txt` 和构建来源证明从 v1.0.0 起随 Release 发布）：

```powershell
Get-FileHash .\RunnerDeck-<版本>-win-x64.zip -Algorithm SHA256
```

## PAT 权限

- **Fine-grained token**（推荐）：Repository access 选中要注册 runner 的仓库；Repository permissions 里 **Administration: Read and write**。
- **Classic token**：勾选 `repo`。

PAT 只保存在 Windows 凭据管理器，不写入文件和日志。

## 功能概览

- 登录一次，列出你的仓库，几步向导把本机注册成某个仓库的 runner（自动下载并校验 actions/runner）。
- 概览页为每个 runner 显示本地状态、当前 job，可单个或全部启停、查看输出、打开目录、移除。
- 设置页：runner 根目录、默认标签、代理、更换 PAT / 退出登录。
- 关闭窗口会先优雅停止所有运行中的 runner 再退出。
- 托盘、开机自启、状态自动轮询在 M3 加入。

## 从源码运行

需要 Windows、Python 3.11 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync
uv run python -m runnerdeck          # 图形界面
uv run runnerdeck-cli --help         # 调试 CLI
uv run pytest                        # 测试（macOS/Linux 上 Windows 专属用例自动跳过）
pwsh scripts/build.ps1               # 打包到 dist\RunnerDeck 和 dist\*.zip
```

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。
