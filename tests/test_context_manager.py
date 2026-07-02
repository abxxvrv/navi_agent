from navi_agent.context.context_manager import ContextManager


class _MemoryStore:
    def get_text(self, target):
        return {
            "memory": "global-note",
            "user": "user-note",
        }[target]


def _write_memory(memories_dir, filename, name, description, body="正文"):
    memories_dir.mkdir(parents=True, exist_ok=True)
    (memories_dir / filename).write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )


def test_system_prompt_renders_memory_and_user(tmp_path):
    manager = ContextManager(workspace=tmp_path, memory_store=_MemoryStore())

    rendered = manager._render_system_prompt_template(
        "{{ NAVI_MEMORY }}|{{ NAVI_USER }}|{{ NAVI_PROJECT_MEMORY }}",
        agents_md="",
        skills_prompt="",
    )

    assert rendered == "global-note|user-note|"


def test_project_memory_index_empty_without_dir(tmp_path):
    manager = ContextManager(workspace=tmp_path)

    assert manager.build_project_memory_prompt() == ""
    assert not (tmp_path / ".navi").exists()


def test_project_memory_index_lists_valid_files_only(tmp_path):
    memories_dir = tmp_path / ".navi" / "memories"
    _write_memory(memories_dir, "test-env.md", "test-env", "测试环境要点")
    # 缺 description，不入索引
    (memories_dir / "broken.md").write_text("---\nname: broken\n---\n正文", encoding="utf-8")
    # 无 frontmatter，不入索引
    (memories_dir / "plain.md").write_text("随手写的笔记", encoding="utf-8")

    manager = ContextManager(workspace=tmp_path)
    index = manager.build_project_memory_prompt()

    assert index == "- [test-env](test-env.md) — 测试环境要点"

    # 索引落盘，且自身被排除在扫描外
    index_file = memories_dir / "PROJECT_memory.md"
    assert index_file.is_file()
    assert "test-env" in index_file.read_text(encoding="utf-8")
    assert manager.build_project_memory_prompt() == index


def test_project_memory_renders_into_system_prompt(tmp_path):
    memories_dir = tmp_path / ".navi" / "memories"
    _write_memory(memories_dir, "conventions.md", "conventions", "项目约定")

    manager = ContextManager(workspace=tmp_path, memory_store=_MemoryStore())
    rendered = manager._render_system_prompt_template(
        "{{ NAVI_PROJECT_MEMORY }}",
        agents_md="",
        skills_prompt="",
    )

    assert rendered == "- [conventions](conventions.md) — 项目约定"


def test_legacy_project_txt_migrates_once(tmp_path):
    memories_dir = tmp_path / ".navi" / "memories"
    memories_dir.mkdir(parents=True)
    (memories_dir / "PROJECT.txt").write_text("约定甲\n§\n约定乙", encoding="utf-8")

    manager = ContextManager(workspace=tmp_path)
    index = manager.build_project_memory_prompt()

    legacy = memories_dir / "legacy-conventions.md"
    assert legacy.is_file()
    content = legacy.read_text(encoding="utf-8")
    assert "name: legacy-conventions" in content
    assert "约定甲\n\n约定乙" in content
    assert "- [legacy-conventions](legacy-conventions.md)" in index
    # 原文件保留
    assert (memories_dir / "PROJECT.txt").is_file()

    # 二次扫描不覆盖已迁移的文件
    legacy.write_text(content.replace("约定甲", "手动改过"), encoding="utf-8")
    manager.build_project_memory_prompt()
    assert "手动改过" in legacy.read_text(encoding="utf-8")
