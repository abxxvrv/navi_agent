"""
处理变量拼接绕过：如果子命令包含变量引用，不认为是 SAFE。
"""

import re

# ─────────────────────────────────────────────────────
# 变量引用检测
# ─────────────────────────────────────────────────────

# 匹配所有形式的变量/命令替换
VAR_REFERENCE_PATTERNS = [
    r'\$\w+',              # $VAR
    r'\$\{[^}]+\}',        # ${VAR}, ${VAR:-default}, ${VAR:=default}
    r'\$\([^)]+\)',        # $(command)
    r'`[^`]+`',            # `command`
]

VAR_REFERENCE_RE = re.compile('|'.join(VAR_REFERENCE_PATTERNS))

def has_variable_reference(cmd: str) -> bool:
    """检测命令是否包含变量引用或命令替换"""
    return bool(VAR_REFERENCE_RE.search(cmd))


# ─────────────────────────────────────────────────────
# 改进的分类逻辑
# ─────────────────────────────────────────────────────

def classify_subcommand_safe(cmd: str) -> tuple[bool, str]:
    """
    判断子命令是否可以视为 SAFE（无需审批）。
    
    返回: (is_safe, reason)
    """
    cmd_stripped = cmd.strip()
    
    # 规则 1: 包含变量引用 → 不安全
    if has_variable_reference(cmd_stripped):
        return False, f"包含变量引用: {cmd_stripped}"
    
    # 规则 2: 包含危险关键字 → 不安全（使用 \b 边界匹配）
    dangerous_patterns = [
        r'\brm\s+-rf?\b', r'\bmkfs\b', r'\bdd\b', 
        r'\bshutdown\b', r'\breboot\b', r'\bkill\s+-9\b'
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, cmd_stripped, re.I):
            return False, f"包含危险关键字: {pattern}"
    
    # 规则 3: 包含 eval/exec → 不安全（可能执行动态内容）
    if re.search(r'\b(eval|exec)\b', cmd_stripped, re.I):
        return False, f"包含 eval/exec"
    
    # 规则 4: 赋值语句本身是 safe 的
    if re.match(r'^[A-Za-z_]\w*=.*$', cmd_stripped):
        return True, "变量赋值"
    
    # 规则 5: 其他情况，检查是否匹配已知 safe 模式
    safe_patterns = [
        r'^echo\b',
        r'^git\s+(add|commit|push|pull|status|log|diff)\b',
        r'^npm\s+(install|run|test|build)\b',
        r'^pip\s+install\b',
        r'^mkdir\b',
        r'^cd\b',
        r'^ls\b',
        r'^cat\b',
        r'^pwd\b',
    ]
    for pattern in safe_patterns:
        if re.match(pattern, cmd_stripped, re.I):
            return True, f"匹配 safe 模式: {pattern}"
    
    # 默认: 不确定，不认为是 safe
    return False, f"未知命令，保守起见不视为 safe"


# ─────────────────────────────────────────────────────
# 测试
# ─────────────────────────────────────────────────────

test_cases = [
    # (命令, 期望 safe?, 原因)
    
    # 变量赋值
    ("A=rm", True, "纯赋值"),
    ("X=/", True, "纯赋值"),
    
    # 变量引用 → 不安全
    ("$A $B $C", False, "变量引用"),
    ("rm -rf $X", False, "变量引用"),
    ("rm -rf ${DIR:-/}", False, "变量引用"),
    ("echo $(whoami)", False, "命令替换"),
    ("echo `whoami`", False, "命令替换"),
    
    # 纯文本命令 → safe
    ("echo hello", True, "纯文本"),
    ("git add .", True, "git 命令"),
    ("npm install", True, "npm 命令"),
    ("mkdir build", True, "mkdir 命令"),
    
    # 危险命令 → 不安全
    ("rm -rf /tmp", False, "危险关键字"),
    ("mkfs.ext4 /dev/sda", False, "危险关键字"),
    
    # eval/exec → 不安全
    ("eval 'rm -rf /'", False, "eval"),
    ("exec rm -rf /", False, "exec"),
]

print("=" * 70)
print(" 子命令安全性分类测试")
print("=" * 70)

all_pass = True
for cmd, expected_safe, reason in test_cases:
    is_safe, actual_reason = classify_subcommand_safe(cmd)
    status = "✓" if is_safe == expected_safe else "✗ FAIL"
    color = "\033[92m" if is_safe == expected_safe else "\033[91m"
    reset = "\033[0m"
    
    if is_safe != expected_safe:
        all_pass = False
    
    print(f"  {color}{status}{reset}: {cmd!r:30s} → {'SAFE' if is_safe else 'UNSAFE':6s} (期望 {'SAFE' if expected_safe else 'UNSAFE':6s}) {reason}")


# ─────────────────────────────────────────────────────
# 完整的分割检查方案
# ─────────────────────────────────────────────────────

print("\n" + "=" * 70)
print(" 完整的分割检查方案演示")
print("=" * 70)

def split_and_check(command: str) -> tuple[bool, list[str]]:
    """
    按逻辑连词分割后逐个检查。
    
    返回: (all_safe, details)
    """
    # 简化版分割（实际应该处理引号）
    parts = re.split(r'\s*&&\s*|\s*\|\|\s*|\s*;\s*', command)
    
    details = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        is_safe, reason = classify_subcommand_safe(part)
        details.append(f"  [{'SAFE' if is_safe else 'UNSAFE':6s}] {part:30s} ← {reason}")
        if not is_safe:
            return False, details
    
    return True, details


test_commands = [
    # 应该放行的
    ("echo hello && echo world", True),
    ("git add . && git commit -m 'msg' && git push", True),
    ("npm install && npm run build && npm test", True),
    ("mkdir build && cd build && cmake ..", True),
    ("X=/; echo hello", True),  # 赋值 + safe 命令
    
    # 应该阻止的（分割后仍有 unsafe）
    ("A=rm; B=-rf; C=/; $A $B $C", False),      # 关键！变量引用
    ("X=/; rm -rf $X", False),                   # 变量引用
    ("echo hello && rm -rf /tmp", False),         # 包含危险命令
    ("eval 'rm -rf /'", False),                   # eval
    ("cd /home && rm -rf .", False),              # 危险命令
    ("echo $(whoami) && echo done", False),       # 命令替换
]

for command, expected in test_commands:
    all_safe, details = split_and_check(command)
    status = "✓" if all_safe == expected else "✗ FAIL"
    color = "\033[92m" if all_safe == expected else "\033[91m"
    reset = "\033[0m"
    
    print(f"\n{color}{status}{reset}: {command}")
    print(f"     期望: {'放行' if expected else '阻止'}, 实际: {'放行' if all_safe else '阻止'}")
    for d in details:
        print(d)


# ─────────────────────────────────────────────────────
# 关键点总结
# ─────────────────────────────────────────────────────

print("\n" + "=" * 70)
print(" 关键防御规则")
print("=" * 70)
print("""
1. 检测变量引用: $VAR, ${VAR}, $(cmd), `cmd`
   → 包含变量引用的子命令一律不视为 SAFE

2. 检测 eval/exec
   → 动态执行的命令一律不视为 SAFE

3. 只有纯文本且匹配已知 safe 模式的命令才视为 SAFE
   → echo, git add, npm install 等

4. 任何不确定的情况都回退到整体检查
""")
