---
name: research-github-project
description: 系统性研究GitHub项目信息的方法论。当用户询问某个开源项目的兼容性、安装方式、平台支持、架构设计、功能特性等问题时使用。适用于：(1) 项目是否支持某个平台/环境 (2) 安装和配置方式 (3) 与上游项目/分支的差异 (4) 项目的实际能力和限制。触发词："能不能用"、"支持XX吗"、"怎么装"、"和XX有什么区别"。
---

# GitHub 项目研究指南

## 研究优先级（按权威性排序）

### 1. 官方文档（最高优先级）
- **README.md / README.zh.md**：项目概述、快速开始
- **官方网站/文档站**：如 `mimo.xiaomi.com/mimocode/install`
- **docs/ 目录**：详细文档

### 2. 项目元数据
- **package.json**：依赖、构建目标、平台特定包
- **Cargo.toml / pyproject.toml**：其他语言的项目配置
- **GitHub Actions**：CI/CD 配置揭示实际测试的平台

### 3. Issues 和 Discussions
- 搜索 `platform:windows`、`os:macos` 等标签
- 查看已关闭的 issues：常见问题通常已有答案
- 注意 maintainer 的官方回复

### 4. 第三方教程（最低优先级，需交叉验证）
- 博客、知乎、CSDN 等教程可能过时或不准确
- 特别注意：教程可能混淆上游项目和 fork 项目

## Fork 项目研究要点

当项目是 fork 时（如 MiMo-Code fork 自 OpenCode）：

1. **明确区分上游项目和 fork 项目的兼容性**
   - 上游项目的限制不一定适用于 fork
   - fork 可能已经解决了上游的某些问题

2. **查看 fork 的改动**
   - `Relationship to XXX` 章节
   - CHANGELOG 或 release notes
   - 实际的代码差异

3. **搜索时使用 fork 项目名称**
   - 搜索 "MiMo-Code Windows" 而非 "OpenCode Windows"

## 平台兼容性研究模板

```
问题：[项目名] 是否支持 [平台]？

1. 官方文档怎么说？
   - README: [结论]
   - 安装页面: [结论]

2. package.json / 构建配置？
   - 是否有平台特定的包（如 @xxx/win32-x64）？
   - CI 测试了哪些平台？

3. Issues 中有相关讨论吗？
   - issue #XX: [结论]

4. 第三方教程怎么说？
   - [来源]: [结论]（需验证）

最终结论：[综合判断]
```

## 常见模式

### Node.js CLI 工具
- 通常通过 npm 安装，跨平台支持较好
- 查看 `optionalDependencies` 中的平台特定包
- 原生模块（如 node-pty）可能有平台限制

### 带有原生二进制的工具
- 查看 `bin/` 目录或 `scripts/` 中的安装脚本
- 检查是否有平台检测逻辑
- 注意 Windows 可能需要 WSL 或特殊处理

### Python 工具
- 通常跨平台，但某些依赖（如系统调用）可能有限制
- 查看 `pyproject.toml` 中的 `requires-python` 和可选依赖

## 输出格式

回答用户时：
1. **先给结论**：支持/不支持/有条件支持
2. **给出依据**：引用官方文档或权威来源
3. **补充细节**：安装方式、注意事项、已知问题
4. **区分来源**：明确说明信息来自官方文档还是第三方
