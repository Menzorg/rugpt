-- Migration 020: Migrate role.model_name to LiteLLM canonical names.
-- All generation roles use Gemma-4 via vLLM through LiteLLM proxy.

UPDATE roles
SET model_name = 'google/gemma-4-31B-it',
    updated_at = NOW()
WHERE model_name IN ('qwen3:14b', 'gpt-oss:20b', 'qwen2.5:7b', 'glm-4.7-flash', 'qwen2:0.5b');
