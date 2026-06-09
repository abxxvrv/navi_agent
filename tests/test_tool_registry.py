"""Tests for tool_registry.py"""

import pytest
from navi_agent.tool_registry import ToolSpec, ToolRegistry


class TestToolSpec:
    """Tests for ToolSpec dataclass."""

    def test_create_tool_spec(self):
        """ToolSpec can be created with all fields."""
        fn = lambda: None
        spec = ToolSpec(name="test", description="desc", parameters={"type": "object"}, function=fn)
        assert spec.name == "test"
        assert spec.description == "desc"
        assert spec.parameters == {"type": "object"}
        assert spec.function is fn


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_init_creates_empty_registry(self):
        """New registry has empty _tools dict."""
        registry = ToolRegistry()
        assert registry._tools == {}

    def test_register_new_tool(self):
        """Register adds tool to registry."""
        registry = ToolRegistry()
        fn = lambda: "result"
        registry.register("tool1", "A tool", {"type": "object"}, fn)

        assert "tool1" in registry._tools
        assert registry._tools["tool1"].name == "tool1"
        assert registry._tools["tool1"].description == "A tool"
        assert registry._tools["tool1"].function is fn

    def test_register_duplicate_raises_error(self):
        """Register raises ValueError on duplicate name."""
        registry = ToolRegistry()
        registry.register("tool1", "desc", {}, lambda: None)

        with pytest.raises(ValueError, match="Tool already registered: tool1"):
            registry.register("tool1", "other desc", {}, lambda: None)

    def test_to_openai_tools_empty_registry(self):
        """Empty registry returns empty list."""
        registry = ToolRegistry()
        assert registry.to_openai_tools() == []

    def test_to_openai_tools_with_tools(self):
        """Returns correct OpenAI format."""
        registry = ToolRegistry()
        registry.register("read_file", "Read a file", {"type": "object", "properties": {}}, lambda: None)

        result = registry.to_openai_tools()
        assert len(result) == 1
        assert result[0] == {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def test_to_openai_tools_multiple_tools(self):
        """Returns all registered tools."""
        registry = ToolRegistry()
        registry.register("tool1", "desc1", {}, lambda: None)
        registry.register("tool2", "desc2", {}, lambda: None)

        result = registry.to_openai_tools()
        assert len(result) == 2
        names = {t["function"]["name"] for t in result}
        assert names == {"tool1", "tool2"}

    def test_invoke_existing_tool(self):
        """Invoke executes the registered function."""
        registry = ToolRegistry()
        registry.register("adder", "Add numbers", {}, lambda a, b: a + b)

        result = registry.invoke("adder", {"a": 1, "b": 2})
        assert result == 3

    def test_invoke_unknown_tool_raises_error(self):
        """Invoke raises ValueError for unknown tool."""
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="Unknown tool: unknown"):
            registry.invoke("unknown", {})

    def test_invoke_passes_arguments(self):
        """Invoke passes arguments to function."""
        registry = ToolRegistry()
        captured = {}

        def spy(**kwargs):
            captured.update(kwargs)
            return "ok"

        registry.register("spy", "Spy tool", {}, spy)
        registry.invoke("spy", {"path": "/tmp", "verbose": True})

        assert captured == {"path": "/tmp", "verbose": True}

    def test_invoke_returns_function_result(self):
        """Invoke returns whatever the function returns."""
        registry = ToolRegistry()
        registry.register("getter", "Get value", {}, lambda: {"status": "ok"})

        result = registry.invoke("getter", {})
        assert result == {"status": "ok"}
