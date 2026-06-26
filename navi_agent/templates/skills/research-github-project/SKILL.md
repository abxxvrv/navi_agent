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

## 系统化研究流程

### Phase 1: 基础信息收集（并行执行）

同时执行以下操作，快速建立项目认知：

1. **web_search**：搜索项目名称 + 关键词（如 "FastContext VRAM requirements"）
2. **web_extract**：提取README页面内容
3. **list_dir**：查看项目目录结构
4. **read_file**：读取关键配置文件（package.json、pyproject.toml、smallcode.toml等）

### Phase 2: 深度分析

根据Phase 1结果，选择关键文件深入分析：

| 文件类型 | 提取信息 |
|----------|----------|
| README.md | 项目定位、核心功能、快速开始 |
| package.json / pyproject.toml | 依赖列表、版本要求、构建配置 |
| ARCHITECTURE.md / docs/ | 架构设计、技术细节 |
| 配置文件（.toml、.env.example） | 配置选项、默认值 |
| 源代码入口（main.py、index.js） | 启动流程、核心逻辑 |

### Phase 3: 资源需求计算

当涉及硬件资源需求时（如显存、内存）：

1. **搜索规格**：使用web_search查找模型参数（层数、头数、维度）
2. **计算公式**：
   - 模型权重 = 参数量 × 每参数字节数
   - KV缓存 = 2 × 层数 × KV头数 × 头维度 × 上下文长度 × 元素字节数
   - 总显存 = 模型权重 + KV缓存 + 系统开销（1-2GB）
3. **量化影响**：Q4约为FP16的1/4，Q8约为FP16的1/2

### Phase 4: 对比分析（如需要）

当用户要求对比多个项目时：

1. **并行研究**：同时收集多个项目的信息
2. **统一维度**：技术栈、架构、功能、资源需求、社区活跃度
3. **表格对比**：使用表格清晰展示差异
4. **给出建议**：基于用户场景推荐最合适的选择

## 常用工具组合

### 模型显存估算
```
web_search: "模型名 VRAM requirements"
web_extract: HuggingFace模型页面
计算: 参数量 × 精度 + KV缓存 + 开销
```

### 项目功能分析
```
web_extract: README.md
read_file: package.json / pyproject.toml
list_dir: src/、bin/、docs/
```

### 安装方式确认
```
web_search: "项目名 install"
web_extract: 安装文档页面
read_file: install.sh / install.ps1
```

## 输出格式

回答用户时：
1. **先给结论**：支持/不支持/有条件支持
2. **给出依据**：引用官方文档或权威来源
3. **补充细节**：安装方式、注意事项、已知问题
4. **区分来源**：明确说明信息来自官方文档还是第三方
5. **量化数据**：如有资源需求，给出具体计算结果

## 项目克隆与运行案例（SmallCode）

当用户要求克隆并运行GitHub项目时，按以下流程执行：

### 步骤1：克隆项目
```bash
# 选择目标目录（如E盘）
cd E:\
git clone https://github.com/Doorman11991/smallcode.git
cd smallcode
```

### 步骤2：检查项目结构
```bash
# 查看关键文件
ls -la package.json README.md .env.example smallcode.toml

# 读取README了解项目
# 读取package.json了解依赖和脚本
```

### 步骤3：安装依赖
```bash
# Node.js项目
npm install

# Python项目
pip install -e .
# 或
uv pip install -e .
```

### 步骤4：配置环境
```bash
# 从示例创建配置文件
cp .env.example .env

# 编辑配置文件
# 设置模型名称、API端点等
```

### 步骤5：运行项目
```bash
# 查看可用脚本
npm run  # Node.js项目

# 运行项目
npm start
# 或
node bin/smallcode.js
```

### 步骤6：测试连接（如需要）
```bash
# 测试API端点
curl -s http://localhost:1234/v1/models

# 测试基本功能
node bin/smallcode.js --help
```

### 常见问题处理
1. **依赖安装失败**：检查Node.js/Python版本，使用镜像源
2. **配置错误**：检查.env文件格式，确保模型名称正确
3. **连接失败**：检查服务器是否运行，端口是否正确
4. **权限问题**：使用管理员权限或修复npm权限
