"""skill_manage 的 patch/delete action 单测。"""

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


def test_delete_removes_entire_skill_directory(tmp_path):
    t = _tool(tmp_path)
    assert t(action="write", name="demo", content=SKILL)["ok"]
    resource = t.skills_dir / "demo" / "examples" / "sample.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("sample", encoding="utf-8")

    r = t(action="delete", name="demo")

    assert r["ok"] is True
    assert r["deleted"] is True
    assert not (t.skills_dir / "demo").exists()
    assert t(action="read", name="demo")["ok"] is False


def test_delete_missing_skill_fails(tmp_path):
    t = _tool(tmp_path)

    r = t(action="delete", name="missing")

    assert r["ok"] is False
    assert "不存在" in r["error"]


def test_delete_refuses_directory_without_skill_file(tmp_path):
    t = _tool(tmp_path)
    target = t.skills_dir / "demo"
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("not a skill", encoding="utf-8")

    r = t(action="delete", name="demo")

    assert r["ok"] is False
    assert target.exists()
    assert (target / "notes.txt").exists()


def test_delete_invalid_name_fails(tmp_path):
    t = _tool(tmp_path)

    r = t(action="delete", name="../demo")

    assert r["ok"] is False
    assert "不合法" in r["error"]
