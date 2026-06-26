---
name: git-clone-troubleshooting
description: 解决git克隆过程中的常见问题，包括网络超时、目录非空、RPC失败、curl错误等。当用户遇到git clone失败、网络连接问题、目录冲突时使用。触发词：git clone失败、网络超时、RPC failed、curl error、目录非空、clone问题。
---

# Git克隆问题排查

## 诊断流程

### 1. 检查目标目录状态
```bash
# 列出目录内容（包括隐藏文件）
ls -la <target-directory>

# 检查是否存在.git目录
ls -la <target-directory>/.git
```

### 2. 处理非空目录
如果目标目录已存在且非空：
- **选项A**：删除目录后重新克隆
  ```bash
  rm -rf <target-directory>
  git clone <repo-url> <target-directory>
  ```
- **选项B**：克隆到子目录
  ```bash
  git clone <repo-url> <target-directory>/<repo-name>
  ```
- **选项C**：如果只有.git目录，删除后重试
  ```bash
  rm -rf <target-directory>/.git
  git clone <repo-url> <target-directory>
  ```

### 3. 网络问题诊断
#### 常见错误及解决方案

**错误1：RPC failed; curl 56 schannel: server closed abruptly**
```bash
# 增加git缓冲区大小
git config --global http.postBuffer 524288000

# 使用浅克隆减少数据传输
git clone --depth 1 <repo-url> <target-directory>

# 设置更长的超时时间（全局配置）
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 600

# 或者使用命令行参数（推荐，不影响全局配置）
git clone --depth 1 --config http.lowSpeedLimit=1000 --config http.lowSpeedTime=60 <repo-url> <target-directory>
```

**错误2：fetch-pack: unexpected disconnect while reading sideband packet**
```bash
# 尝试不同的协议
git clone git://github.com/user/repo.git  # 使用git协议 instead of https

# 或者使用SSH（如果已配置）
git clone git@github.com:user/repo.git
```

**错误3：Connection timed out**
```bash
# 检查网络连接
ping github.com
curl -I https://github.com

# 使用代理（如果需要）
git config --global http.proxy http://proxy:port
git config --global https.proxy http://proxy:port
```

### 4. 检查部分下载
如果克隆中断，检查是否有部分下载的文件：
```bash
# 检查目标目录是否有.git目录（部分克隆）
ls -la <target-directory>/.git

# 检查ZIP下载文件大小（如果使用curl下载）
ls -lh <file>.zip

# 删除不完整的.git目录后重试
rm -rf <target-directory>/.git
git clone <repo-url> <target-directory>
```

### 5. 高级调试
```bash
# 启用详细输出
GIT_TRACE=1 git clone <repo-url> <target-directory>

# 检查git配置
git config --list --global

# 临时禁用SSL验证（不推荐，仅用于调试）
git config --global http.sslVerify false
```

## 最佳实践

### 克隆前检查
1. 确认目标目录不存在或为空
2. 检查网络连接稳定性
3. 确认有足够的磁盘空间

### 网络优化
1. 使用浅克隆（--depth 1）获取最新代码
2. 增加http.postBuffer大小
3. 考虑使用git协议或SSH

### 错误恢复
1. 如果克隆中断，删除目标目录重试
2. 检查.git目录完整性
3. 使用`git fsck`检查仓库完整性

## 常见场景

### 场景1：克隆到现有目录
```bash
# 检查目录内容
ls -la existing-dir/

# 如果只有无关文件，删除后克隆
rm -rf existing-dir/
git clone <repo-url> existing-dir/

# 如果需要保留文件，克隆到子目录
git clone <repo-url> existing-dir/new-project/
```

### 场景2：网络不稳定
```bash
# 使用浅克隆
git clone --depth 1 <repo-url> <target-directory>

# 如果失败，增加缓冲区后重试
git config --global http.postBuffer 1048576000
git clone --depth 1 <repo-url> <target-directory>
```

### 场景3：大型仓库
```bash
# 部分克隆（Git 2.22+）
git clone --filter=blob:none <repo-url> <target-directory>

# 稀疏检出
git clone --sparse <repo-url> <target-directory>
cd <target-directory>
git sparse-checkout init --cone
git sparse-checkout set <subdirectory>
```

## 恢复损坏的克隆
```bash
# 检查仓库完整性
cd <target-directory>
git fsck --full

# 如果objects损坏，重新克隆
cd ..
rm -rf <target-directory>
git clone <repo-url> <target-directory>
```

## 用户偏好处理
当用户要求"先找到路径，再确认"时：
1. 使用`list_dir`检查目标路径
2. 报告目录状态和完整路径
3. 等待用户确认后再执行克隆
4. 提供多个选项（如克隆到子目录或清空目录）