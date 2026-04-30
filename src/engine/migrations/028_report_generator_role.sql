-- Migration 028: report_generator role for AI-driven evening reports.
-- Role-only (no system user) — used internally by TaskReportService via AgentExecutor.

DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Генератор отчётов',
        'report_generator',
        'AI-ассистент для генерации вечерних отчётов руководителю на основе ответов сотрудников в утренних опросах',
        'Вы — AI-аналитик, который пишет вечерние отчёты руководителю.',
        'google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'report_generator.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;
END $$;
