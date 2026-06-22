---
name: windows-cli-global-access
description: >
  让 Windows 上的 CLI 工具在任意路径下直接输入命令启动。
  当用户问"如何让 XX 命令全局可用"、"在任意目录输入 XX 启动"、"添加到 PATH"、
  "文件资源管理器地址栏输入命令"时使用。
  适用于 Python venv 中的 CLI 工具、npm 全局工具、uv tool、任何需要全局访问的命令行程序。
  触发词：全局可用、PATH、任意路径、地址栏、环境变量、where、which。
---

# Windows CLI 工具全局可用

## 快速诊断

```bash
# 检查命令位置
where <command>       # Windows CMD
which <command>       # Git Bash / PowerShell

# 检查 Python 环境
which python && which pip
python --version

# 检查 npm 全局路径
npm root -g           # 包目录
npm config get prefix # 全局前缀

# 检查 uv tool 路径
uv tool dir           # 工具数据目录
```

## 方案选择

### 方案 0：uv tool（Python CLI，推荐）

**最佳隔离**：每个工具独立虚拟环境，零冲突，速度极快。

```bash
# 安装
uv tool install <package-name>
uv tool install git+https://github.com/owner/repo.git  # 从 GitHub

# 临时运行（不安装）
uvx <command>

# 管理
uv tool list
uv tool uninstall <package-name>
```

**路径**：
- 二进制：`~/.local/bin/`（需确保在 PATH 中）
- 数据：`%LOCALAPPDATA%\uv\tools\`
- 自定义：`UV_INSTALL_DIR="E:\uv"`, `UV_TOOL_DIR="E:\uv\tools"`

### 方案 1：npm global（Node.js CLI）

```bash
# 全局安装
npm install -g <package-name>
npm install -g @scope/package-name  # scoped 包

# 临时运行（不安装）
npx <command>
npx @scope/<command>

# 管理
npm list -g --depth=0
npm uninstall -g <package-name>
```

**路径查询**：
```bash
npm root -g          # node_modules 目录
npm config get prefix # 全局前缀（bin 在此下）
```

**nvm4w 用户**：
- 版本目录：`E:\nvm\vXX.XX.X\`
- 当前版本符号链接：`C:\nvm4w\nodejs\` → `E:\nvm\vXX.XX.X\`
- 切换版本：`nvm use XX.XX.X`（秒切，只改符号链接）

### 方案 2：pipx（Python CLI，传统）

```bash
pipx install <package-name>
pipx list
pipx uninstall <package-name>
```

**与 uv tool 对比**：uv tool 更快，功能相同。已有 uv 则无需 pipx。

### 方案 3：用户级 PATH（通用）

**GUI**：`Win+R` → `sysdm.cpl` → 高级 → 环境变量 → 用户变量 Path → 编辑 → 新建

**PowerShell**：
```powershell
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$newPath = "$currentPath;E:\new\path"
[Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
```

**生效**：重新打开终端。文件资源管理器地址栏也需重新打开窗口。

### 方案 4：PowerShell Profile Wrapper（最灵活）

```powershell
# $PROFILE 位置
# ~/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1
function mytool {
    & E:\path\to\mytool.exe @args
}
```

**限制**：只在 PowerShell 有效，文件资源管理器地址栏无效。

### 方案 5：Git Bash ~/bin

```bash
mkdir -p ~/bin
ln -s /path/to/tool.exe ~/bin/tool
```

**限制**：只在 Git Bash 有效。

## 文件资源管理器地址栏

地址栏使用**系统 PATH**，不是 shell profile。必须用方案 3（用户 PATH）。

## 验证

```bash
# PowerShell
Get-Command <command>
where <command>

# 测试
<command> --help
```

## 常见问题

**Q: 加了 PATH 但命令找不到？**
A: 重新打开终端。环境变量修改不会立即生效。

**Q: Python 和 pip 指向不同环境？**
A: 用 `python -m pip` 确保装到正确环境。或用 uv tool 完全隔离。

**Q: 多个项目有同名工具？**
A: 用 uv tool（方案 0）或 PowerShell wrapper（方案 4）避免冲突。

**Q: npm 全局包装在哪？**
A: 运行 `npm root -g` 查看。通常是 `%APPDATA%\npm\node_modules\` 或自定义目录。
