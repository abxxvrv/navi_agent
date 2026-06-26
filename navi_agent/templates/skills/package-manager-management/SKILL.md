---
name: package-manager-management
description: 管理 uv、npm、pip 等包管理工具，包括安装、配置、检查版本、管理已安装包、环境隔离等。当用户询问"uv tool 怎么用"、"npm global 装了什么"、"pip 包管理"、"工具链管理"、"环境隔离"时使用。也适用于：检查包管理工具版本、列出已安装包、安装新包、配置缓存目录、迁移包存储位置。触发词：uv、npm、pip、包管理、工具链、环境隔离、全局安装、global install。
---

# 包管理工具管理指南

## 1. uv 工具管理

uv 是 Python 包管理工具，支持工具安装、环境隔离等。

### 检查 uv 环境

```bash
# 检查 uv 版本
uv --version

# 检查 uv 工具目录
uv tool dir

# 列出已安装的 uv 工具
uv tool list
```

### uv 工具操作

```bash
# 安装工具
uv tool install <package>

# 升级工具
uv tool upgrade <tool>

# 升级所有工具
uv tool upgrade --all

# 卸载工具
uv tool uninstall <tool>

# 运行工具命令
uv tool run <command>
```

### uv 工具安装示例

```bash
# 安装 CLI-Anything Hub（Python CLI 工具管理器）
uv tool install cli-anything-hub

# 安装后检查
cli-hub --version
cli-hub list

# 安装其他常用工具
uv tool install ruff  # Python linter
uv tool install black  # 代码格式化
uv tool install mypy  # 类型检查
```

### uv 工具目录结构

默认工具目录：`E:\uv\tools`（可通过 `UV_TOOL_DIR` 环境变量配置）

每个工具都有三种格式：无扩展名（Unix）、`.cmd`（Windows）、`.ps1`（PowerShell）

### uv 缓存管理

```bash
# 查看缓存目录
uv cache dir

# 清理缓存
uv cache clean
```

环境变量：
- `UV_CACHE_DIR`：缓存目录
- `UV_TOOL_DIR`：工具目录
- `UV_PYTHON_INSTALL_DIR`：Python 安装目录

## 2. npm 全局包管理

### 检查 npm 环境

```bash
# 检查 npm 版本
npm --version

# 检查全局安装路径
npm root -g

# 列出全局安装的包
npm list -g --depth=0
```

### npm 全局包操作

```bash
# 安装全局包
npm install -g <package>

# 升级全局包
npm update -g <package>

# 升级所有全局包
npm update -g

# 卸载全局包
npm uninstall -g <package>
```

### npm 全局包目录

Windows 默认目录：`C:\Users\<user>\AppData\Roaming\npm\npm-global`

目录结构：
```
npm-global/
├── node_modules/      # 包文件
├── package-name.cmd   # Windows 命令脚本
├── package-name       # Unix 脚本
└── package-name.ps1   # PowerShell 脚本
```

### npm 缓存管理

```bash
# 查看缓存位置
npm config get cache

# 清理缓存
npm cache clean --force
```

### npm 项目依赖管理

当用户问"依赖是项目依赖吗"、"npm install装在哪里"、"node_modules是什么"时使用。

**本地安装（项目依赖）**：
```bash
# 在项目目录中安装依赖
cd my-project
npm install

# 依赖安装在项目目录的node_modules文件夹中
# 创建package-lock.json锁定版本
```

**本地 vs 全局安装**：
| 方面 | 本地安装 | 全局安装 |
|------|----------|----------|
| **位置** | `项目目录/node_modules/` | `系统目录/npm-global/` |
| **用途** | 项目运行必需 | CLI工具 |
| **版本** | 每个项目独立 | 系统统一 |
| **命令** | `npm install` | `npm install -g` |

**node_modules目录**：
- 包含所有项目依赖
- 可能很大（几百MB）
- **不要**提交到Git（已在.gitignore中）
- 删除后可重新安装：`npm install`

**package-lock.json**：
- 自动生成
- 锁定确切的依赖版本
- 确保团队使用相同版本

**检查项目依赖**：
```bash
# 查看依赖树
npm list --depth=0

# 检查特定包
npm list <package>

# 检查过时包
npm outdated
```

**清理和重建**：
```bash
# 删除node_modules
rm -rf node_modules

# 重新安装依赖
npm install

# 清理npm缓存
npm cache clean --force
```

## 3. pip 包管理

### 检查 pip 环境

```bash
# 检查 pip 版本
pip --version

# 列出已安装的包
pip list

# 查看包信息
pip show <package>
```

### pip 包操作

```bash
# 安装包
pip install <package>

# 升级包
pip install --upgrade <package>

# 卸载包
pip uninstall <package>

# 从 requirements.txt 安装
pip install -r requirements.txt
```

### pip 缓存管理

```bash
# 查看缓存目录
pip cache dir

# 清理缓存
pip cache purge
```

## 4. 环境隔离最佳实践

### 使用虚拟环境

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境（Windows Git Bash）
source .venv/Scripts/activate

# 激活虚拟环境（PowerShell）
.venv\Scripts\Activate.ps1
```

### 使用 uv 管理 Python 版本

```bash
# 列出可用 Python 版本
uv python list

# 安装 Python 版本
uv python install 3.12

# 设置项目 Python 版本
uv python pin 3.12
```

### 工具链隔离策略

1. **全局工具**：使用 `uv tool install` 或 `npm install -g` 安装常用 CLI 工具
2. **项目依赖**：使用虚拟环境（`venv` 或 `uv venv`）隔离项目依赖
3. **临时工具**：使用 `uvx`（uv 的临时工具运行器）或 `npx` 运行一次性工具

## 5. 常见问题排查

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| `command not found` | 工具未安装或不在 PATH 中 | 检查安装路径，添加到 PATH |
| 权限不足 | 需要管理员权限 | 使用管理员权限运行，或使用用户级安装 |
| 版本冲突 | 多个版本共存 | 使用虚拟环境隔离，或指定版本安装 |
| 缓存损坏 | 缓存文件损坏 | 清理缓存后重新安装 |

## 6. 工具链健康检查

## 7. 用户环境偏好

用户偏好：
- **安装位置**：偏好非系统盘（如 E 盘），避免占用 C 盘空间
- **环境隔离**：强意识，要求工具链隔离，不混用全局和项目依赖
- **工具管理**：偏好使用 uv tool 管理 Python CLI 工具，npm 管理 Node.js 工具
- **缓存目录**：配置到非系统盘（如 `E:\uv\cache`）

```bash
# 检查所有包管理工具
echo "=== uv ===" && uv --version
echo "=== npm ===" && npm --version
echo "=== pip ===" && pip --version
echo "=== node ===" && node --version
echo "=== python ===" && python --version

# 检查全局工具
echo "=== uv tools ===" && uv tool list
echo "=== npm global ===" && npm list -g --depth=0
```