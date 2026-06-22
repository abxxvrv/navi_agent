"""skill_manage 的 patch action 单测。"""

# 先导入 runtime 包，按生产顺序加载 tools.builtin，避免 builtin↔runtime 的预存循环导入
# 在 builtin 被首个导入时触发。
import navi_agent.runtime  # noqa: F401

from navi_agent.skills.skill_manage import SkillManageTool

SKILL = """---
name: demo
description: a demo skill
---

# Demo
step one
step two
"""


def _tool(tmp_path):
    """构造一个把 skills_dir 指向临时目录的工具，绕开 NAVI_HOME。"""
    t = SkillManageTool()
    t.skills_dir = tmp_path / "skills"
    t._patcher.workspace = t.skills_dir.resolve()
    return t


def test_patch_unique_match(tmp_path):
    t = _tool(tmp_path)
    assert t(action="write", name="demo", content=SKILL)["ok"]

    r = t(action="patch", name="demo", old_text="step one", new_text="step ONE")
    assert r["ok"] is True
    assert r["replacements"] == 1
    assert "step ONE" in t(action="read", name="demo")["content"]


def test_patch_not_found(tmp_path):
    t = _tool(tmp_path)
    t(action="write", name="demo", content=SKILL)

    r = t(action="patch", name="demo", old_text="nonexistent", new_text="x")
    assert r["ok"] is False


def test_patch_multiple_requires_replace_all(tmp_path):
    t = _tool(tmp_path)
    # 让 "step one" 出现两次
    t(action="write", name="demo", content=SKILL.replace("step two", "step one"))

    r = t(action="patch", name="demo", old_text="step one", new_text="x")
    assert r["ok"] is False

    r2 = t(action="patch", name="demo", old_text="step one", new_text="x", replace_all=True)
    assert r2["ok"] is True
    assert r2["replacements"] == 2


def test_patch_breaking_frontmatter_rolls_back(tmp_path):
    t = _tool(tmp_path)
    t(action="write", name="demo", content=SKILL)

    # 删掉 description 行 → frontmatter 不完整 → 应回滚
    r = t(action="patch", name="demo", old_text="description: a demo skill\n", new_text="")
    assert r["ok"] is False
    assert "description: a demo skill" in t(action="read", name="demo")["content"]
