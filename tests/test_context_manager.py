from navi_agent.context.context_manager import ContextManager


class _MemoryStore:
    def get_text(self, target):
        return {
            "memory": "global-note",
            "user": "user-note",
            "project": "project-note",
        }[target]


def test_system_prompt_renders_project_memory(tmp_path):
    manager = ContextManager(workspace=tmp_path, memory_store=_MemoryStore())

    rendered = manager._render_system_prompt_template(
        "{{ NAVI_MEMORY }}|{{ NAVI_USER }}|{{ NAVI_PROJECT_MEMORY }}",
        agents_md="",
        skills_prompt="",
    )

    assert rendered == "global-note|user-note|project-note"
