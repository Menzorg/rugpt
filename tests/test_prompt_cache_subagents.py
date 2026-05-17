from types import SimpleNamespace

from src.engine.services.prompt_cache import PromptCache


def test_get_prompt_prefers_subagent_prompt_when_requested(tmp_path):
    prompts_dir = tmp_path / "prompts"
    subagents_dir = prompts_dir / "subagents"
    subagents_dir.mkdir(parents=True)
    (prompts_dir / "pm.md").write_text("root pm prompt", encoding="utf-8")
    (subagents_dir / "pm.md").write_text("subagent pm prompt", encoding="utf-8")

    cache = PromptCache(str(prompts_dir))
    role = SimpleNamespace(code="pm", prompt_file="pm.md", system_prompt="db prompt")

    assert cache.get_prompt(role, is_subagent=True) == "subagent pm prompt"


def test_get_prompt_falls_back_to_root_prompt_for_subagents(tmp_path):
    prompts_dir = tmp_path / "prompts"
    subagents_dir = prompts_dir / "subagents"
    subagents_dir.mkdir(parents=True)
    (prompts_dir / "reasoner.md").write_text("root reasoner prompt", encoding="utf-8")

    cache = PromptCache(str(prompts_dir))
    role = SimpleNamespace(code="reasoner", prompt_file="reasoner.md", system_prompt="db prompt")

    assert cache.get_prompt(role, is_subagent=True) == "root reasoner prompt"
