"""
演示：按逻辑连词分割命令后逐个检查的方案。

思路：如果每个子命令都已经 safe 或在 allowlist 中，整体放行。
问题：是否存在分割后每个子命令都 safe，但组合起来危险的情况？
"""

import re
import shlex
from dataclasses import dataclass
from enum import Enum

# ─────────────────────────────────────────────────────
# 逻辑连词定义
# ─────────────────────────────────────────────────────

# Bash 逻辑连词（按优先级排序）
LOGIC_OPERATORS = {
    ";":   "sequential",    # 顺序执行，无条件
    "&&":  "and",           # 前一个成功才执行
    "||":  "or",            # 前一个失败才执行
    "|":   "pipe",          # 管道（stdout → stdin）
    "&":   "background",    # 后台执行
}

def split_by_logic_operators(command: str) -> list[tuple[str, str | None]]:
    """
    按逻辑连词分割命令。
    
    返回: [(子命令, 连词类型), ...]
    最后一个子命令的连词类型为 None。
    
    注意：需要处理引号内的连词不分割。
    """
    result = []
    current = []
    i = 0
    
    # 简化版：用正则分割（不处理引号嵌套）
    # 实际实现应该用 shlex 或状态机
    pattern = r'(?<!["\'])\s*(&&|\|\||[;&|])\s*(?!["\'])'
    
    parts = re.split(pattern, command)
    
    # parts 交替为: [子命令1, 连词1, 子命令2, 连词2, ...]
    for j, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        if part in LOGIC_OPERATORS:
            continue  # 跳过连词本身
        # 找到对应的连词
        op_type = None
        if j + 1 < len(parts) and parts[j + 1].strip() in LOGIC_OPERATORS:
            op_type = LOGIC_OPERATORS[parts[j + 1].strip()]
        elif j > 0 and parts[j - 1].strip() in LOGIC_OPERATORS:
            op_type = LOGIC_OPERATORS[parts[j - 1].strip()]
        result.append((part, op_type))
    
    return result


# ─────────────────────────────────────────────────────
# 模拟检查
# ─────────────────────────────────────────────────────

class Risk(Enum):
    SAFE = "safe"
    RISKY = "risky"
    DANGEROUS = "dangerous"

# 简化的危险模式
DANGEROUS_KEYWORDS = ["rm -rf", "mkfs", "dd", "shutdown", "reboot", "kill -9", "fork"]
RISKY_KEYWORDS = ["rm", "chmod 777", "git push --force", "sudo"]

def classify_subcommand(cmd: str) -> Risk:
    cmd_lower = cmd.lower().strip()
    for kw in DANGEROUS_KEYWORDS:
        if kw in cmd_lower:
            return Risk.DANGEROUS
    for kw in RISKY_KEYWORDS:
        if kw in cmd_lower:
            return Risk.RISKY
    return Risk.SAFE

def check_whole_command(command: str) -> tuple[Risk, str]:
    """整体检查（现有逻辑）"""
    return classify_subcommand(command), "整体匹配"

def check_split_command(command: str) -> tuple[Risk, list[str]]:
    """分割后逐个检查"""
    parts = split_by_logic_operators(command)
    details = []
    max_risk = Risk.SAFE
    
    for sub_cmd, op_type in parts:
        risk = classify_subcommand(sub_cmd)
        details.append(f"  [{risk.value}] {sub_cmd}" + (f" (op: {op_type})" if op_type else ""))
        if risk.value == "dangerous":
            max_risk = Risk.DANGEROUS
        elif risk.value == "risky" and max_risk != Risk.DANGEROUS:
            max_risk = Risk.RISKY
    
    return max_risk, details


# ─────────────────────────────────────────────────────
# 测试用例
# ─────────────────────────────────────────────────────

test_cases = [
    # (命令, 描述, 分割后是否应该放行)
    
    # 正常 case：分割后每个都 safe
    ("echo hello && echo world", "两个 safe 命令", True),
    ("git add . && git commit -m 'msg' && git push", "三个 git 操作", True),
    ("npm install && npm run build && npm test", "npm 工作流", True),
    ("mkdir build && cd build && cmake ..", "构建流程", True),
    
    # 正常 case：分割后有一个 dangerous，应该阻止
    ("echo hello && rm -rf /tmp/x", "safe + dangerous", False),
    ("cd /tmp && rm -rf *", "safe + dangerous", False),
    
    # 危险 case：分割后每个都 safe，但组合起来危险！
    ("X=/; rm -rf $X", "变量赋值 + 使用", False),  # 分割后: X=/ (safe) + rm -rf $X (safe??)
    ("DIR=/tmp; rm -rf $DIR/*", "变量 + 删除", False),
    
    # 危险 case：&& 的条件执行
    ("false && rm -rf / || echo safe", "条件执行陷阱", False),
]

print("=" * 70)
print(" 按逻辑连词分割后逐个检查 vs 整体检查")
print("=" * 70)

for command, desc, should_pass in test_cases:
    print(f"\n{'─' * 60}")
    print(f"命令: {command}")
    print(f"描述: {desc}")
    print(f"期望: {'放行' if should_pass else '阻止'}")
    
    # 整体检查
    whole_risk, _ = check_whole_command(command)
    print(f"\n整体检查: {whole_risk.value}")
    
    # 分割检查
    split_risk, details = check_split_command(command)
    print(f"分割检查: {split_risk.value}")
    for d in details:
        print(d)
    
    # 判断
    split_would_pass = split_risk == Risk.SAFE
    if split_would_pass and not should_pass:
        print(f"\n  ⚠️ 危险！分割后会被放行，但实际上应该阻止！")
    elif not split_would_pass and should_pass:
        print(f"\n  ✗ 分割后被阻止，但应该放行（误报）")
    else:
        print(f"\n  ✓ 行为正确")


# ─────────────────────────────────────────────────────
# 更深入的危险例子分析
# ─────────────────────────────────────────────────────

print("\n" + "=" * 70)
print(" 危险反例深度分析")
print("=" * 70)

dangerous_examples = [
    {
        "cmd": "X=/; rm -rf $X",
        "why": "变量赋值 X=/ 看起来是 safe 的，但后续 rm -rf $X 实际是 rm -rf /",
        "split_safe": ["X=/", "rm -rf $X"],
        "real_effect": "rm -rf /",
    },
    {
        "cmd": "false && rm -rf / || echo ok",
        "why": "false 失败 → 跳过 rm -rf / → 执行 echo ok。分割后 rm -rf / 被检查但实际上不会执行",
        "split_safe": ["false", "rm -rf /", "echo ok"],
        "real_effect": "rm -rf / 永远不会执行（因为 false 总是失败）",
    },
    {
        "cmd": "true || rm -rf / && echo safe",
        "why": "true 成功 → 跳过 rm -rf / → 执行 echo safe。分割后 rm -rf / 被检查但不会执行",
        "split_safe": ["true", "rm -rf /", "echo safe"],
        "real_effect": "rm -rf / 永远不会执行（因为 true 总是成功）",
    },
    {
        "cmd": "A=rm; B=-rf; C=/; $A $B $C",
        "why": "每个赋值都是 safe 的，但组合起来是 rm -rf /",
        "split_safe": ["A=rm", "B=-rf", "C=/", "$A $B $C"],
        "real_effect": "rm -rf /",
    },
    {
        "cmd": "cd /home && rm -rf .",
        "why": "cd /home 是 safe 的，rm -rf . 看起来是删除当前目录（safe?），但组合起来删 /home",
        "split_safe": ["cd /home", "rm -rf ."],
        "real_effect": "rm -rf /home",
    },
]

for ex in dangerous_examples:
    print(f"\n{'─' * 60}")
    print(f"命令: {ex['cmd']}")
    print(f"为什么危险: {ex['why']}")
    print(f"分割后子命令: {ex['split_safe']}")
    print(f"实际效果: {ex['real_effect']}")


# ─────────────────────────────────────────────────────
# 结论
# ─────────────────────────────────────────────────────

print("\n" + "=" * 70)
print(" 结论")
print("=" * 70)
print("""
方案: 按逻辑连词分割后逐个检查

✓ 适用场景:
  - 所有子命令都是 SAFE 级别（如 echo、git add、npm install）
  - 用户明确批准过的 scope 类命令（如 pip install）
  - 简单的 &&/|| 链式操作

✗ 不适用场景:
  - 包含变量赋值 + 使用的命令
  - 包含条件执行（&& ||）且中间有危险命令
  - 子命令之间有状态依赖（cd 后的相对路径）
  - 需要理解 shell 展开语义的命令

建议:
  1. 分割检查作为"快速放行"的优化路径
  2. 如果分割后所有子命令都是 SAFE → 直接放行
  3. 如果有任何子命令不是 SAFE → 回退到整体检查（现有逻辑）
  4. 这样既优化了常见 case，又不引入新的安全漏洞
""")
