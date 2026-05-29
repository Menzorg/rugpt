# Context Explosion Defense: Technical Documentation

## Scope
This document describes the current multi-layer defense against context window explosion across:
- runtime state (`src/engine/agents/runtime.py`)
- executor (`src/engine/agents/executor.py`)
- simple graph wiring (`src/engine/agents/graphs/simple.py`)
- middleware (`src/engine/agents/middleware.py`)
- document tools (`src/engine/agents/tools/list_documents.py`)
- RAG tool (`src/engine/agents/tools/rag_tool.py`)
- token counting (`src/engine/utils/token_counter.py`)
- prompt-level steering (`src/engine/prompts/...`)

## High-level design
The defense is layered to prevent runaway token growth during iterative tool use:
1. Initial budget accounting in executor before first model step.
2. Shared per-run runtime counters and cap.
3. Tool-level hard block when budget is exhausted.
4. Middleware-level history compaction before model calls.
5. Middleware-level tool call count limits.
6. Prompt-level behavior constraints to reduce unbounded tool loops.

This combines soft mitigation (compaction, concise listing behavior) with hard stops (critical cap checks, tool call limits).

## Core runtime state
`RuntimeContext` is the single source of truth for budget tracking in one agent run.

Fields relevant to context defense:
- `total_tokens_spent`: cumulative token estimate spent in this run.
  - Includes: prompt+messages sent at start, plus tool outputs appended later.
- `critical_tokens_cap`: hard per-run cap for expensive retrieval stages.
  - Current default: `24_000`.
- `lock`: async lock to serialize budget-sensitive tool mutations.
- `available_tools_count`: used by token estimator in middleware paths that include tool schema overhead.
- `list_documents_runtime_data`:
  - `seen_ids` for cross-call deduplication.
  - `spent_summary_tokens` for summary-specific sub-budgeting.
- `rag_search_runtime_data`:
  - `chunk_ids` for seen-chunk memory and retrieval narrowing logic.

## Token counting model
`token_counter` provides three primitives:
- `count_tokens(text, tool_count=0)`
- `count_tokens_messages(messages)`
- `cut_text_by_token_count(text, limit)`

Details:
- Prefers local `tokenizers/tokenizer.json` if available.
- Fallback: `tiktoken` `cl100k_base` with a compensation multiplier (`*0.8`).
- Optional tool schema overhead estimate: `tool_count * 150`.

Important: different call sites use different representations (`str` blob vs message objects). Estimates are intentionally approximate and defensive.

## Executor stage: initial budget seeding
Before agent execution, executor:
1. Builds final message list (original + injected context: user metadata, org context, summary).
2. Serializes messages into `messages_blob`.
3. Adds `count_tokens(messages_blob)` to `runtime_context.total_tokens_spent`.

Effect:
- RAG/list tools see current prompt weight before doing any retrieval.
- Hard guard can trigger on first tool call if prompt is already heavy.

Executor also installs middleware:
- `HistoryCompactionMiddleware(trigger_tokens=23000, keep_last=12)`
- `ToolCallLimitMiddleware` for selected tools:
  - `rag_search`: max 15 calls per run.
  - `list_global_documents` / `list_private_documents`: max 5 calls per run.
  - task tools total: max 50 calls per run.

## Middleware stage
### 1) HistoryCompactionMiddleware (active)
Purpose: reduce conversation size when nearing context limit.

Behavior:
- Runs in `abefore_model`.
- Counts tokens for non-system messages.
- If token count `< trigger_tokens` (currently 23000), no action.
- If exceeded:
  1. Keep last `keep_last` messages (currently 12).
  2. Summarize older messages via LLM using `agent_compaction_summary.md`.
  3. Replace full history with one `[CONVERSATION SUMMARY]` AI message + kept tail.
  4. Recompute tokens for replacement messages.
  5. Reset `runtime.context.total_tokens_spent` to that recomputed value.

Why reset matters:
- Prevents stale pre-compaction totals from permanently blocking tools.
- Aligns hard cap checks with actual post-compaction state.

### 2) TokenBudgetToolBlockMiddleware (present, currently not wired by executor)
Purpose: block tools based on ratio of estimated tokens to max context.

Current status:
- Implemented but not appended in current executor path.
- Uses `count_tokens(..., tool_count=available_tools_count)` and threshold ratio (`0.85` of `30000`).

## Tool stage hard guards
Both `rag_search` and list-doc tools enforce `RuntimeContext` cap checks (`total_tokens_spent` vs `critical_tokens_cap`).

### rag_search
Flow:
1. Validate context, service init, ACL, indexed status.
2. Under `runtime.context.lock`:
   - If `total_tokens_spent >= critical_tokens_cap`: return hard-block message.
   - Else run retrieval, format result, count tokens, increment `total_tokens_spent`.

Extra anti-explosion behavior:
- Keeps seen chunk ids in runtime.
- Reduces `top_k` from 4 to 3 after enough seen chunks.

### list_global_documents / list_private_documents
Flow:
1. Visibility filtering and optional search filters.
3. Guard: if `total_tokens_spent >= critical_tokens_cap`, return hard-block message.
4. On successful output, count returned text tokens and increment `total_tokens_spent`.

Additional containment inside list tools:
- Cross-call dedup (`seen_ids`) avoids repeating same documents.
- Summary sub-budget (`_SUMMARY_TOKENS_BUDGET=4000`) across run.
- Per-item summary cap (`2.5%` of summary budget).
- Pagination (`page`, `_PAGE_SIZE=50`) for both search and list flows.
- Compact mode when summary budget is exhausted (omit summary/date/size-heavy fields).
- `file_id` path bypasses budgets/filters and returns single-document detail.
- Global visibility is now: other users' files only (`uploaded_by_user_id != caller`) with public-only baseline; admin fallback may include non-public files of others when needed.

## Prompt-level defenses
### Compaction prompt (`src/engine/prompts/agent_compaction_summary.md`)
Defines strict summarization contract for context compression:
- Preserve IDs, filenames, chunk indices, constraints, unresolved blockers.
- Avoid hallucinated facts.
- Produce structured sections to retain operational state.

Impact:
- Helps compaction preserve retrievability-critical references while shrinking tokens.

### Tool prompts (`src/engine/prompts/tools/*.md`)
They enforce call discipline:
- `rag_search.md`: call limit guidance (15).
- `list_global_documents.md` / `list_private_documents.md`: call limit guidance (5), requirement to use retrieval pipeline properly.

### Role prompt behavior (`doc_search.md`)
Contains an aggressive search algorithm and "use tools to max limits" wording.
This increases retrieval pressure and makes runtime/middleware hard defenses essential.

## End-to-end lifecycle of token budget
1. Executor builds final input messages.
2. Executor seeds `total_tokens_spent` from message blob.
3. Agent starts with middleware stack.
4. Before each model step, compaction may trigger and shrink history.
5. If compaction runs, `total_tokens_spent` is recalibrated to compacted history.
6. Tool calls run; each retrieval output increments `total_tokens_spent`.
7. Once `total_tokens_spent >= critical_tokens_cap`, retrieval tools hard-stop with explicit message.
8. Tool call limit middleware separately constrains repeated invocations regardless of token count.

## Current thresholds and limits
- `RuntimeContext.critical_tokens_cap`: `24_000`
- `HistoryCompactionMiddleware.trigger_tokens`: `23_000`
- `HistoryCompactionMiddleware.keep_last`: `12`
- `ToolCallLimitMiddleware`:
  - `rag_search`: `15`
  - `list_global_documents`: `5`
  - `list_private_documents`: `5`
  - task tools total: `50`
- list summary sub-budget:
  - total: `4_000`
  - per item max fraction: `0.025`

## Concurrency and correctness notes
- Shared runtime lock is used in `rag_search` for budget/state mutation.
- `list_*_documents` currently mutates runtime counters without taking `runtime.context.lock`.
- Executor sets `RunnableConfig(max_concurrency=2)`, so lock discipline is required.

## Failure and degradation behavior
- If compaction summary generation fails, middleware logs exception and skips compaction (no crash).
- If tokenizer file is missing, token counting falls back to tiktoken.
- If tool/service/context validation fails, tools return explicit user-facing fallback messages.

## Known gaps / maintenance risks
1. Budget accounting is estimate-based, not model-provider exact accounting.
2. Initial seeding uses a plain role/content blob; middleware uses message-object counting.
3. `TokenBudgetToolBlockMiddleware` exists but is not currently active in executor.
4. Prompts still include strategy text that can encourage many tool calls; runtime safeguards currently absorb this pressure.
5. `doc_search.md` references legacy `list_documents` naming while runtime wiring uses `list_global_documents` and `list_private_documents`.
6. Tool prompt `list_global_documents.md` still mentions `owner_id` filter behavior, but runtime tool schema currently does not expose `owner_id`.

## Practical tuning guidance
- To make defense stricter: lower `critical_tokens_cap` or `trigger_tokens`, and/or lower tool call limits.
- To make defense less strict: increase cap/trigger with caution; verify compaction quality and retrieval utility.
- Keep `trigger_tokens < critical_tokens_cap` so compaction can happen before hard tool blocking.
- Any cap change should be validated with long multi-tool conversations and concurrent tool-call scenarios.

## Quick checklist for future changes
When adding any new retrieval-heavy tool:
1. Increment `runtime_context.total_tokens_spent` by returned content tokens.
2. Guard with `total_tokens_spent >= critical_tokens_cap` before heavy retrieval.
3. Use runtime lock around state mutation and token accounting.
4. Add tool call limit middleware if repeated calls are likely.
5. Ensure prompt guidance does not force unbounded broad scans.
