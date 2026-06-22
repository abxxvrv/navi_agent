__all__ = ["AgentRuntime"]


def __getattr__(name):
    # 懒加载 AgentRuntime，避免 import 该包（如经由 runtime.interrupt）时急切拉入
    # agent → tools.builtin，造成 builtin ↔ runtime 循环导入。
    if name == "AgentRuntime":
        from .agent import AgentRuntime

        return AgentRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
