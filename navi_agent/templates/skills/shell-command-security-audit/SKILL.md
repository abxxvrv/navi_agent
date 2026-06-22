---
name: shell-command-security-audit
description: 审计基于正则的 shell 命令安全模式，发现绕过路径，设计更健壮的命令审批机制。当用户问"这个正则能不能绕过"、"命令安全检测有没有漏洞"、"怎么防止 shell 命令注入"时使用。也适用于：设计命令审批系统、实现命令白名单/黑名单、按逻辑连词分割命令做细粒度检查。触发词：绕过、bypass、命令审计、shell 安全、正则匹配、命令审批、approval、危险命令、硬阻断。
---

# Shell 命令安全审计

## 核心问题

正则匹配的是**原始命令文本**，但 shell 执行时会做**变量展开、路径规范化、引号剥离、命令替换**。这个语义鸿沟是所有绕过的根源。

```
正则看到的:  rm -rf ${X:-/}
Shell 执行:  rm -rf /
```

## 审计方法论

### 1. 提取所有 pattern 的 description

每个 description 就是一个"类别"，批准一个 description 会放行所有匹配该 description 的命令。

```bash
# 从源码提取
grep -E '^\s+\(r.*, "' tools/approval.py | sed 's/.*,\s*"//' | sed 's/".*//'
```

### 2. 按绕过类别系统测试

| 类别 | 绕过方式 | 示例 |
|------|---------|------|
| 路径等价 | `/./` `//` `..` | `rm -rf /./` 等价 `rm -rf /` |
| 变量展开 | `$VAR` `${VAR:-default}` | `X=/; rm -rf $X` |
| 命令替换 | `$(cmd)` `` `cmd` `` | `rm -rf $(echo /)` |
| 引号剥离 | `'/'` `"/"` | `rm -rf '/'` shell 去引号 |
| eval 包装 | `eval 'cmd'` | `eval 'rm -rf /'` |
| 通配符 glob | `/?` `[a-z]` | `rm -rf /?` 展开为单字符目录 |
| 符号链接 | `ln -s /target link && rm link` | 间接删除 |
| 函数名变体 | `f(){ f\|f& };f` | fork bomb 换名 |
| 完整路径 | `/usr/sbin/shutdown` | 绕过命令位置检测 |

### 3. 编写测试脚本模板

```python
def test_command(cmd, expected_blocked=True, note=""):
    is_blocked, desc = detect(cmd)
    bypassed = not is_blocked and expected_blocked
    print(f"  {'⚠ BYPASS' if bypassed else '✕ BLOCKED' if is_blocked else '✓ OK'}: {cmd!r}")
    return bypassed

# 测试路径等价
test_command("rm -rf /.")      # /. → /
test_command("rm -rf /./")     # /./ → /
test_command("rm -rf //")      # // → /
test_command("rm -rf /tmp/../") # /tmp/../ → /

# 测试变量展开
test_command("X=/; rm -rf $X")
test_command("rm -rf ${X:-/}")
test_command("rm -rf $(echo /)")
test_command("rm -rf `echo /`")

# 测试引号
test_command("rm -rf '/'")
test_command('rm -rf "/"')

# 测试 eval
test_command("eval 'rm -rf /'")
```

## 命令分割审批设计

### 动机

`git add . && git commit -m 'msg' && git push` 包含连词，整体检查会把简单的 git 操作变成需要审批。按逻辑连词分割后逐个检查可以放行这类 case。

### Bash 逻辑连词

| 连词 | 含义 | 前一个失败时 | 
|------|------|-------------|
| `;` | 顺序执行 | 继续执行 |
| `&&` | 逻辑与 | 跳过下一个 |
| `\|\|` | 逻辑或 | 执行下一个 |
| `\|` | 管道 | 两个都运行 |
| `&` | 后台 | 两个都运行 |

### 分割检查算法

```python
def check_command_split_first(command: str) -> Decision:
    # 1. 按逻辑连词分割
    parts = split_by_logic_operators(command)
    
    # 2. 逐个检查
    for sub_cmd in parts:
        if not is_subcommand_safe(sub_cmd):
            return check_command_whole(command)  # 回退整体检查
    
    # 3. 全部 SAFE → 直接放行
    return ALLOW
```

### 子命令安全性判断

**关键规则：包含变量引用的子命令一律不视为 SAFE。**

```python
VAR_REFERENCE_RE = re.compile('|'.join([
    r'\$\w+',           # $VAR
    r'\$\{[^}]+\}',     # ${VAR}, ${VAR:-default}
    r'\$\([^)]+\)',     # $(command)
    r'`[^`]+`',         # `command`
]))

def is_subcommand_safe(cmd: str) -> bool:
    # 规则 1: 包含变量引用 → 不安全
    if VAR_REFERENCE_RE.search(cmd):
        return False
    
    # 规则 2: 包含 eval/exec → 不安全
    if re.search(r'\b(eval|exec)\b', cmd):
        return False
    
    # 规则 3: 包含危险关键字（用 \b 边界匹配）
    if re.search(r'\brm\s+-rf?\b|\bmkfs\b|\bdd\b|\bshutdown\b', cmd):
        return False
    
    # 规则 4: 赋值语句本身是 safe 的
    if re.match(r'^[A-Za-z_]\w*=.*$', cmd):
        return True
    
    # 规则 5: 匹配已知 safe 模式
    safe_patterns = [
        r'^echo\b', r'^git\s+(add|commit|push|pull|status)\b',
        r'^npm\s+(install|run|test|build)\b', r'^mkdir\b', r'^cd\b',
    ]
    return any(re.match(p, cmd, re.I) for p in safe_patterns)
```

### 危险反例（不能分割检查的情况）

| 命令 | 分割后每个都 safe？ | 实际效果 |
|------|-------------------|---------|
| `A=rm; B=-rf; C=/; $A $B $C` | 赋值 safe，`$A $B $C` 有变量引用 → **被拦截** | ✓ 安全 |
| `cd /home && rm -rf .` | `cd` safe，`rm -rf` dangerous → **被拦截** | ✓ 安全 |
| `false && rm -rf / \|\| echo ok` | `rm -rf /` dangerous → **被拦截** | ✓ 安全（但 rm 实际不会执行） |

## 修复建议优先级

1. **路径规范化**（低成本，解决 `/./` `//` `..` 问题）
2. **检测变量引用后回退整体检查**（中成本，解决 `$VAR` 问题）
3. **关键字匹配用 `\b` 边界**（低成本，避免 `add` 误匹配 `dd`）
4. **seccomp/LD_PRELOAD 拦截系统调用**（高成本，根本解决）
5. **容器沙箱**（中成本，已有 docker/singularity 时直接用）
