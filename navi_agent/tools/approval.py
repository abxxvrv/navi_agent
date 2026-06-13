from __future__ import annotations

import json
import re
import shlex
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

# 三个枚举类
# 三种审批模式
class ApprovalMode(str, Enum):
    STRICT = "strict"
    NORMAL = "normal"
    OPEN = "open"

# 三种决策
class ApprovalAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

# 五种风险等级
class RiskLevel(str, Enum):
    SAFE = "safe"
    UNKNOWN = "unknown"
    RISKY = "risky"
    DANGEROUS = "dangerous"
    HARD_DENY = "hard_deny"

# 用户三种选择
class UserApprovalChoice(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    REJECT = "reject"

# 数据类
@dataclass(frozen=True)
class ApprovalDecision:
    action: ApprovalAction
    risk: RiskLevel
    reason: str
    tool_name: str
    tool_args: dict[str, Any]
    approval_key: str | None = None
    command: str | None = None

    @property
    def is_allow(self) -> bool:
        return self.action == ApprovalAction.ALLOW

    @property
    def is_ask(self) -> bool:
        return self.action == ApprovalAction.ASK

    @property
    def is_deny(self) -> bool:
        return self.action == ApprovalAction.DENY

    def to_tool_error(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.reason,
            "approval": {
                "action": self.action.value,
                "risk": self.risk.value,
                "tool_name": self.tool_name,
                "command": self.command,
            },
        }


class ApprovalManager:
    """
    Navi 的安全审批管理器。

    职责：
    - 不执行工具
    - 不读取用户输入
    - 只判断当前工具调用应该 allow / ask / deny
    - 维护本会话 allowlist

    推荐调用位置：
        AgentRuntime._tool_node()
            -> approval_manager.check_tool_call(...)
            -> 如果 allow，再 tool_registry.invoke(...)
            -> 如果 ask，交给 CLI 回调询问用户
            -> 如果 deny，直接返回工具失败
    """

    COMMAND_TOOLS = {
        "run_command"
    }

    READ_ONLY_TOOLS = {
        "get_date",
        "get_weather",
        "read_file",
        "list_dir",
        "list_directory",
        "grep",
        "glob",
        "view_file",
        "skill_view",
        "skill_manage",
        "list_skills",
        "list_sessions",
        "web_search",
        "web_extract",
        "memory",
        "vision_analyze",
    }

    WRITE_TOOLS = {
        "write_file",
        "patch_file",
        "edit_file",
        "str_replace",
        "replace_file",
        "create_file",
        "delete_file",
        "move_file",
        "copy_file",
        "apply_patch",
    }

    # 命令参数列表
    COMMAND_ARG_KEYS = (
        "command",
    )

    PATH_ARG_KEYS = (
        "path",
        "file_path",
        "target_path",
        "filename",
        "file",
    )

    ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

    # 正则模式表
    # 永远拒绝的命令
    HARD_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"(^|\s)(sudo\s+)?rm\s+.*(-r|-rf|-fr)\s+/(?:\s|$|\*)", re.I),
            "命中硬阻断：禁止递归删除系统根目录。",
        ),
        (
            re.compile(r"(^|\s)(sudo\s+)?rm\s+.*--no-preserve-root", re.I),
            "命中硬阻断：禁止使用 --no-preserve-root。",
        ),
        (
            re.compile(r"\bmkfs(?:\.\w+)?\b", re.I),
            "命中硬阻断：禁止格式化文件系统。",
        ),
        (
            re.compile(r"\bdd\s+.*\bof=/dev/(sd|hd|vd|nvme|disk)\w+", re.I),
            "命中硬阻断：禁止向磁盘设备直接写入。",
        ),
        (
            re.compile(r":\s*\(\)\s*\{\s*:\|:&\s*\};:", re.I),
            "命中硬阻断：禁止执行 fork bomb。",
        ),
        (
            re.compile(r"\bformat\s+[a-z]:", re.I),
            "命中硬阻断：禁止格式化 Windows 磁盘。",
        ),
        (
            re.compile(r"\b(del|erase)\b.*\b(/s|/q)\b.*\b[a-z]:\\?", re.I),
            "命中硬阻断：禁止递归删除 Windows 磁盘根目录。",
        ),
        (
            re.compile(r"\b(rmdir|rd)\b.*\b/s\b.*\b/q\b.*\b[a-z]:\\?", re.I),
            "命中硬阻断：禁止递归删除 Windows 磁盘根目录。",
        ),
        (
            re.compile(r"\bRemove-Item\b.*(-Recurse|-r)\b.*(-Force|-fo)\b.*\b[a-z]:\\?", re.I),
            "命中硬阻断：禁止强制递归删除 Windows 磁盘根目录。",
        ),
    ]

    # 危险命令
    DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"\brm\s+.*(-r|-rf|-fr)\b", re.I),
            "该命令会递归删除文件或目录。",
        ),
        (
            re.compile(r"\b(del|erase|rmdir|rd)\b", re.I),
            "该命令会删除文件或目录。",
        ),
        (
            re.compile(r"\bRemove-Item\b", re.I),
            "该命令会删除文件或目录。",
        ),
        (
            re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
            "git reset --hard 会丢弃未提交修改。",
        ),
        (
            re.compile(r"\bgit\s+clean\s+.*(-f|-d)\b", re.I),
            "git clean 会删除未跟踪文件。",
        ),
        (
            re.compile(r"\bgit\s+push\s+.*--force\b", re.I),
            "git push --force 可能覆盖远端历史。",
        ),
        (
            re.compile(r"\bgit\s+checkout\s+--\s+\.", re.I),
            "该命令可能丢弃当前工作区修改。",
        ),
        (
            re.compile(r"\bgit\s+branch\s+(-D|--delete\s+--force)\b", re.I),
            "该命令会强制删除 Git 分支。",
        ),
        (
            re.compile(r"\b(chmod|chown)\s+.*-R\b", re.I),
            "该命令会递归修改权限或所有者。",
        ),
        (
            re.compile(r"\bchmod\s+.*777\b", re.I),
            "chmod 777 会放开文件权限。",
        ),
        (
            re.compile(r"\bsudo\b", re.I),
            "该命令会使用管理员权限执行。",
        ),
        (
            re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh|fish)\b", re.I),
            "该命令会下载并直接执行远程脚本。",
        ),
        (
            re.compile(r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b.*\|\s*(iex|Invoke-Expression)\b", re.I),
            "该命令会下载并直接执行远程 PowerShell 脚本。",
        ),
        (
            re.compile(r"\b(shutdown|reboot|halt|poweroff|Stop-Computer|Restart-Computer)\b", re.I),
            "该命令会关机或重启系统。",
        ),
        (
            re.compile(r"\breg\s+delete\b", re.I),
            "该命令会删除 Windows 注册表项。",
        ),
    ]

    # 有风险的命令
    RISKY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"\b(npm|pnpm|yarn)\s+(install|add|remove|uninstall)\b", re.I),
            "该命令会修改依赖或 lockfile。",
        ),
        (
            re.compile(r"\b(pip|pip3|uv\s+pip|poetry|pdm)\s+(install|add|remove|uninstall)\b", re.I),
            "该命令会安装或修改 Python 依赖。",
        ),
        (
            re.compile(r"\b(git\s+add|git\s+commit|git\s+merge|git\s+rebase|git\s+checkout|git\s+switch)\b", re.I),
            "该 Git 命令可能修改仓库状态。",
        ),
        (
            re.compile(r"\b(mv|move|cp|copy|xcopy|robocopy)\b", re.I),
            "该命令会移动或复制文件。",
        ),
        (
            re.compile(r"\b(mkdir|New-Item|touch)\b", re.I),
            "该命令会创建文件或目录。",
        ),
        (
            re.compile(r"\b(sed\s+-i|perl\s+-pi)\b", re.I),
            "该命令会原地修改文件。",
        ),
        (
            re.compile(r"(^|[^>])>{1,2}([^>]|$)", re.I),
            "该命令包含重定向，可能写入文件。",
        ),
        (
            re.compile(r"\b(tee|Out-File|Set-Content|Add-Content)\b", re.I),
            "该命令可能写入文件内容。",
        ),
        (
            re.compile(r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b", re.I),
            "该命令会访问网络。",
        ),
        (
            re.compile(r"\b(bash|sh|zsh|fish|powershell|pwsh)\s+(-c|-Command)\b", re.I),
            "该命令会执行内联 shell 脚本。",
        ),
        (
            re.compile(r"\b(python|python3|py|node|ruby|perl)\s+(-c|-e)\b", re.I),
            "该命令会执行内联脚本。",
        ),
        (
            re.compile(r"\b(docker|podman|kubectl|terraform)\b", re.I),
            "该命令可能影响容器、集群或基础设施。",
        ),
    ]

    # 安全命令
    SAFE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"^(pwd|cd|ls|dir|tree|whoami|date|ver)\b", re.I),
            "常见只读命令。",
        ),
        (
            re.compile(r"^(cat|type|head|tail|more|less|Get-Content)\b", re.I),
            "读取文件内容。",
        ),
        (
            re.compile(r"^(rg|grep|findstr|Select-String)\b", re.I),
            "搜索文本内容。",
        ),
        (
            re.compile(r"^git\s+(status|diff|log|show|branch|remote)\b", re.I),
            "只读 Git 查询命令。",
        ),
        (
            re.compile(r"^(python|python3|py|node|npm|pnpm|yarn|pip|pip3)\s+--?version\b", re.I),
            "查看工具版本。",
        ),
        (
            re.compile(r"^(pytest|python\s+-m\s+pytest|py\s+-m\s+pytest|uv\s+run\s+pytest)\b", re.I),
            "运行测试命令。",
        ),
        (
            re.compile(r"^(npm|pnpm|yarn)\s+(test|run\s+test)\b", re.I),
            "运行测试脚本。",
        ),
    ]

    def __init__(
        self,
        mode: str | ApprovalMode = ApprovalMode.NORMAL,
        workspace: str | Path = ".",
        navi_home: str | Path | None = None,
    ) -> None:
        self.mode = self._coerce_mode(mode)
        self.workspace = Path(workspace).resolve()
        self.navi_home = Path(navi_home).resolve() if navi_home is not None else None
        self.session_allowlist: set[str] = set()

    # 入口函数，根据工具名分派到四种处理
    def check_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
    ) -> ApprovalDecision:
        args = tool_args or {}
        normalized_tool_name = self._normalize_tool_name(tool_name)

        if normalized_tool_name in self.COMMAND_TOOLS:
            return self._check_command_tool(normalized_tool_name, args)

        # 只读工具，允许执行。
        if normalized_tool_name in self.READ_ONLY_TOOLS:
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=RiskLevel.SAFE,
                reason="只读工具，允许执行。",
                tool_name=tool_name,
                tool_args=args,
                approval_key=None,
            )

        if normalized_tool_name in self.WRITE_TOOLS:
            return self._check_write_tool(normalized_tool_name, tool_name, args)

        # MCP 工具，用户配置即信任。
        if normalized_tool_name.startswith("mcp_"):
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=RiskLevel.SAFE,
                reason="MCP 工具，用户配置即信任。",
                tool_name=tool_name,
                tool_args=args,
                approval_key=None,
            )

        # 未分类工具
        return ApprovalDecision(
            action=ApprovalAction.DENY,
            risk=RiskLevel.HARD_DENY,
            reason="工具未在审批分类中，拒绝执行。",
            tool_name=tool_name,
            tool_args=args,
            approval_key=None,
        )

    # 把用户选择转换成 bool，同时处理"本会话允许"的白名单逻辑。
    def resolve_user_choice(
        self,
        decision: ApprovalDecision,
        choice: str | UserApprovalChoice,
    ) -> bool:

        if choice == UserApprovalChoice.ALLOW_ONCE:
            return True

        if choice == UserApprovalChoice.ALLOW_SESSION:
            self.session_allowlist.add(decision.approval_key) # 把这个加入白名单
            return True

        return False

    # 对命令的危险等级分类
    def classify_command(self, command: str) -> tuple[RiskLevel, str]:
        normalized = self._normalize_command(command) # 标准化

        if not normalized:
            return RiskLevel.UNKNOWN, "空命令或无法识别的命令。"

        # 按优先级匹配四张表
        for pattern, reason in self.HARD_DENY_PATTERNS:
            if pattern.search(normalized):
                return RiskLevel.HARD_DENY, reason

        for pattern, reason in self.DANGEROUS_PATTERNS:
            if pattern.search(normalized):
                return RiskLevel.DANGEROUS, reason

        for pattern, reason in self.RISKY_PATTERNS:
            if pattern.search(normalized):
                return RiskLevel.RISKY, reason

        for pattern, reason in self.SAFE_PATTERNS:
            if pattern.search(normalized):
                return RiskLevel.SAFE, reason

        return RiskLevel.UNKNOWN, "未知 shell 命令，保守起见需要用户确认。"

    # 生成命令行工具的白名单 key。常见重复命令使用 scope，其他命令回退到 exact。
    def make_command_approval_key(self, command: str) -> str:
        normalized_command = self._normalize_command(command)
        try:
            tokens = shlex.split(normalized_command, posix=False)
        except ValueError:
            tokens = normalized_command.split()

        normalized_tokens = [token.strip("\"'").lower() for token in tokens]
        scopes = [
            (("pip", "install"), "shell:scope:pip install"),
            (("pip3", "install"), "shell:scope:pip install"),
            (("python", "-m", "pip", "install"), "shell:scope:pip install"),
            (("python3", "-m", "pip", "install"), "shell:scope:pip install"),
            (("py", "-m", "pip", "install"), "shell:scope:pip install"),
            (("uv", "pip", "install"), "shell:scope:uv pip install"),
            (("npm", "install"), "shell:scope:npm install"),
            (("npm", "i"), "shell:scope:npm install"),
            (("pnpm", "install"), "shell:scope:pnpm install"),
            (("pnpm", "add"), "shell:scope:pnpm add"),
            (("yarn", "install"), "shell:scope:yarn install"),
            (("yarn", "add"), "shell:scope:yarn add"),
            (("git", "add"), "shell:scope:git add"),
            (("git", "commit"), "shell:scope:git commit"),
            (("git", "push"), "shell:scope:git push"),
        ]

        for prefix, approval_key in scopes:
            if tuple(normalized_tokens[: len(prefix)]) == prefix:
                return approval_key

        return f"shell:exact:{normalized_command}"

    def make_write_approval_key(self, tool_args: dict[str, Any]) -> str:
        path = self._extract_path(tool_args)
        if not path:
            args_fingerprint = self._stable_args_fingerprint(tool_args)
            return f"tool:write:args:{args_fingerprint}"

        input_path = Path(path)
        target = input_path.resolve() if input_path.is_absolute() else (self.workspace / input_path).resolve()

        if self.navi_home is not None and target.is_relative_to(self.navi_home):
            return "tool:write:scope:navi_home"

        if target.is_relative_to(self.workspace):
            return "tool:write:scope:workspace"

        external_scope = target if target.exists() and target.is_dir() else target.parent
        return f"tool:write:scope:external:{external_scope}"

    def is_session_allowed(self, approval_key: str | None) -> bool:
        if not approval_key:
            return False
        return approval_key in self.session_allowlist

    # 检查命令类工具（terminal, shell, bash 等），根据命令内容和审批模式决定 allow / ask / deny。
    def _check_command_tool(
        self,
        normalized_tool_name: str,
        args: dict[str, Any],
    ) -> ApprovalDecision:
        command = self._extract_command(args)
        risk, reason = self.classify_command(command)
        approval_key = self.make_command_approval_key(command)

        if risk == RiskLevel.HARD_DENY:
            return ApprovalDecision(
                action=ApprovalAction.DENY,
                risk=risk,
                reason=reason,
                tool_name=normalized_tool_name,
                tool_args=args,
                approval_key=None,
                command=command,
            )

        if self.mode == ApprovalMode.OPEN:
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=risk,
                reason="open 模式：除硬阻断命令外，自动允许执行。",
                tool_name=normalized_tool_name,
                tool_args=args,
                approval_key=approval_key,
                command=command,
            )

        if self.is_session_allowed(approval_key):
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=risk,
                reason="该命令已在本会话中被允许。",
                tool_name=normalized_tool_name,
                tool_args=args,
                approval_key=approval_key,
                command=command,
            )

        if self.mode == ApprovalMode.STRICT:
            return ApprovalDecision(
                action=ApprovalAction.ASK,
                risk=risk,
                reason="strict 模式：所有 shell 命令都需要审批。",
                tool_name=normalized_tool_name,
                tool_args=args,
                approval_key=approval_key,
                command=command,
            )

        if risk == RiskLevel.SAFE:
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=risk,
                reason=reason,
                tool_name=normalized_tool_name,
                tool_args=args,
                approval_key=approval_key,
                command=command,
            )

        return ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=risk,
            reason=reason,
            tool_name=normalized_tool_name,
            tool_args=args,
            approval_key=approval_key,
            command=command,
        )

    def _check_write_tool(
        self,
        normalized_tool_name: str,
        original_tool_name: str,
        args: dict[str, Any],
    ) -> ApprovalDecision:
        approval_key = self.make_write_approval_key(args)

        if self.mode == ApprovalMode.OPEN:
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=RiskLevel.RISKY,
                reason="open 模式：自动允许写入类工具。",
                tool_name=original_tool_name,
                tool_args=args,
                approval_key=approval_key,
            )

        if self.is_session_allowed(approval_key):
            return ApprovalDecision(
                action=ApprovalAction.ALLOW,
                risk=RiskLevel.RISKY,
                reason="该写入操作已在本会话中被允许。",
                tool_name=original_tool_name,
                tool_args=args,
                approval_key=approval_key,
            )

        return ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="该工具会修改文件系统，需要用户审批。",
            tool_name=original_tool_name,
            tool_args=args,
            approval_key=approval_key,
        )

    # 提取命令
    def _extract_command(self, args: dict[str, Any]) -> str:
        for key in self.COMMAND_ARG_KEYS: # 遍历参数列表，找到第一个有值的。
            value = args.get(key)
            if value is None:
                continue

            if isinstance(value, str):
                return value

            if isinstance(value, list):
                return " ".join(str(item) for item in value)

            return str(value)

        return ""

    def _extract_path(self, args: dict[str, Any]) -> str | None:
        for key in self.PATH_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _normalize_command(self, command: str) -> str:
        command = command.replace("\x00", "")
        command = self.ANSI_RE.sub("", command)
        command = unicodedata.normalize("NFKC", command)
        command = command.strip()

        try:
            parts = shlex.split(command, posix=False)
            if parts:
                command = " ".join(parts)
        except ValueError:
            pass

        command = re.sub(r"\s+", " ", command)
        return command.strip()

    # 把工具名标准化：去空格、转小写、连字符转下划线
    def _normalize_tool_name(self, tool_name: str) -> str:
        return tool_name.strip().lower().replace("-", "_")

    def _stable_args_fingerprint(self, args: dict[str, Any]) -> str:
        try:
            return json.dumps(args, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return repr(args)

    def _coerce_mode(self, mode: str | ApprovalMode) -> ApprovalMode:
        if isinstance(mode, ApprovalMode):
            return mode

        normalized = mode.strip().lower()

        if normalized in {"strict", "safe"}:
            return ApprovalMode.STRICT

        if normalized in {"normal", "default"}:
            return ApprovalMode.NORMAL

        if normalized in {"open", "yolo", "unsafe"}:
            return ApprovalMode.OPEN

        raise ValueError(
            f"Unknown approval mode: {mode!r}. "
            "Expected one of: strict, normal, open."
        )
