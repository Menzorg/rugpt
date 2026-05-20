# Invoice flow & generic modal protocol

**Status:** Design draft
**Date:** 2026-05-19
**Authors:** Petr Bezdenezhnykh (product), Alexandr (infra discussion), Claude (architect)

## Problem

Two intertwined needs from the alpha customer (ООО Профессиональные технологии):

1. **Boss inbox for invoices.** Workers send invoice files to the manager (Эдуард). He validates each — approve or reject. On the invoice's due date the accountant must be reminded to actually process the payment. Today this lives in chat, mail, and pieces of paper.

2. **AI must not write data, but must influence workflows.** Generally: AI roles have only read-only tools to avoid hallucination-driven mutations. But for invoice approval, the AI is the natural front-door (it sees the file, summarises it, hands it to the boss). The boss must decide and trigger the state change — without the AI ever calling a write tool.

The second need is **not invoice-specific** — it is a general pattern for any "AI prepares, human decides" workflow (approving tenders, confirming contracts, closing tasks, marking acknowledgements). The infrastructure must therefore be a generic modal protocol, with invoices as the first consumer.

## Non-goals

- LangGraph interrupt/checkpointer support (the "G-approach" discussed with Alexandr). This may come later when chain decisions are needed. v1 is tool-based ("T-approach").
- Bitrix or other external system integration. Invoices live in our DB only.
- OCR or structured field extraction (amount, supplier). Summary from existing RAG pipeline is enough — the manager decides from the summary; if unclear, opens the file.
- Multi-step modal flows ("approve, then enter a comment, then choose category"). Modal carries a single decision per click.
- Per-department head access to subordinate's invoices. v1: only admin sees all; everyone else sees only their own.

## Architecture

Three independent blocks, composed into the invoice flow:

### A. Modal protocol (generic infrastructure)

- **Engine tool** `show_modal(title, body, actions=[{label, action_type, params}])`. Read-only — emits a structured payload, mutates nothing.
- **Storage**: payload lives in `messages.metadata.modal` JSONB on the AI's message that contained the tool call. No separate `modal_requests` table — modal state derives from the target resource's state in its own table (see "State derivation").
- **Action registry** (Python in-memory module on engine): each `action_type` is registered with `handler`, `params_schema` (Pydantic), `permission(user, params) → bool`. The registry is populated at engine startup.
- **Dispatcher endpoint** `POST /api/v1/actions/{action_type}` (webclient proxy: `/api/actions/{action_type}`). Validates params, checks permission, calls handler, returns result. Unknown `action_type` → 404.
- **Per-role whitelist**: each `Role.agent_config.allowed_action_types` lists which action types this role may emit. Engine validates `show_modal` calls against the caller role's whitelist; rejection mid-call. Defence against prompt injection — an AI role can't be tricked into showing modals it isn't registered to handle.
- **Frontend modal renderer**: subscribed to chat WebSocket events. On a `message` with `metadata.modal` payload, renders an overlay modal. Buttons fire `POST /api/actions/{action_type}` with `params` from the payload. After click: buttons disabled, spinner, then result. On chat refresh / second viewer / device, the modal's "resolved" state is derived from the target resource's status, not from a separate row.

### B. Invoices module

- **Table `invoices`** (new):
  ```sql
  CREATE TABLE invoices (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      file_id UUID NOT NULL REFERENCES user_files(id),
      uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
      due_date DATE,                                  -- entered manually at upload
      status VARCHAR(20) NOT NULL DEFAULT 'created',  -- created | approved | rejected | processed
      approved_by_user_id UUID REFERENCES users(id),
      approved_at TIMESTAMPTZ,
      rejected_at TIMESTAMPTZ,
      rejection_reason TEXT,
      processed_at TIMESTAMPTZ,
      processed_by_user_id UUID REFERENCES users(id),
      is_active BOOLEAN NOT NULL DEFAULT true,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
  CREATE INDEX idx_invoices_org_status ON invoices(org_id, status) WHERE is_active = true;
  CREATE INDEX idx_invoices_due_date ON invoices(due_date)
      WHERE status = 'approved' AND processed_at IS NULL AND is_active = true;
  ```
- **`organizations.accountant_user_id` UUID NULL** — admin-selected recipient of accountant notifications. ON DELETE SET NULL.
- **`messages.metadata` JSONB NOT NULL DEFAULT '{}'** — added if not present, hosts `metadata.modal` payload (and reusable for future metadata).
- **Reuse existing `user_files` + RAG pipeline.** Worker uploads file → standard `user_files` row → RAG ingest (Tika + LLM summary) → `user_files.summary` becomes the invoice's human-readable description. No separate OCR.
- **Status flow**: `created → approved | rejected` (admin action); `approved → processed` (accountant action). Soft-delete via `is_active`.
- **Routes** (engine, namespaced `/api/v1/invoices/`):
  - `POST /` — multipart upload (file + `due_date` form field). Any active user in org. Creates user_file + invoice row.
  - `GET /` — list with filters `?status=`, `?uploaded_by_user_id=`. Permission rule below.
  - `GET /{id}` — single. Same permission rule.
  - Action endpoints handled via `POST /api/v1/actions/...` (not separate /invoices/{id}/approve), per the generic dispatcher.

### C. AI role `invoice_clerk` (consumer of A)

- Lives in **system org** `00000000-0000-0000-0000-000000000000`, like PM.
- Visible in all orgs through the existing "ИИ" sidebar group and `@@`-mention picker. Whitelist in Sidebar.tsx and ChatInput.tsx expands to include `username='invoice_clerk'` alongside `'pm'`.
- Tools: `list_invoices`, `get_invoice` (both read-only, permission-aware), `show_modal`.
- `agent_config.allowed_action_types = ["invoice_approve", "invoice_reject", "invoice_mark_processed"]`.
- `agent_type = 'simple'` (single ReAct agent with tools — what PM and all system-org roles actually use; supervisor is for multiagent subagent orchestration which invoice_clerk does not need).
- `prompt_file = 'invoice_clerk.md'` — prompt describes the role: "you help find invoices, summarise them, and propose approve/reject modals to the manager".

### D. Scheduler — accountant notifications

- New job in `SchedulerService`, runs once per day per org in the org's local morning hours.
- For each org where `accountant_user_id IS NOT NULL`:
  - Find invoices with `status='approved' AND processed_at IS NULL AND due_date IN (today_local, today_local + 1 day) AND is_active = true`.
  - For each, create an `in_app_notification` for `accountant_user_id` with type `invoice_due`, content = invoice summary, reference_type='invoice', reference_id=invoice.id.
  - Idempotency: same invoice should not get duplicate notifications for the same calendar day. Either dedupe by `notification_log` (does the existing log fit?) or add a `last_notified_at` field on invoices.

## State derivation (modal lifecycle)

Modal state is NOT stored separately. Frontend derives it from the target resource:

- Modal payload includes `target: { type: 'invoice', id: <uuid> }`.
- On render, frontend fetches `GET /invoices/{id}` (or relies on a cached store) to read current `status`.
- If `status === 'created'` → modal renders with active action buttons.
- If `status` is anything else → modal renders as "resolved" (greyed, showing outcome and resolved_by/at).
- Idempotency for double-click: frontend disables buttons after first click; backend handler additionally rejects with 409 if the invoice is no longer in `status='created'` (for approve/reject) or `status='approved'` (for mark_processed).

## Permissions matrix

| Operation | Who |
|---|---|
| `POST /invoices` (upload) | Any active user in org |
| `GET /invoices`, `GET /invoices/{id}` | Admin sees all in org. Non-admin sees only `uploaded_by_user_id = self.id`. |
| Action `invoice_approve` | Admin only (handler checks `user.is_admin`) |
| Action `invoice_reject` | Admin only |
| Action `invoice_mark_processed` | `user.id == org.accountant_user_id OR user.is_admin` |
| `PATCH /organizations/{id}` with `accountant_user_id` | Admin only |
| AI tool `list_invoices` / `get_invoice` | Applies same view permission as the route, scoped to the calling user's identity from `RunnableConfig.configurable.caller_user_id` |

## Action registry shape

```python
# engine/actions/registry.py
from typing import Callable
from pydantic import BaseModel

class ActionDefinition:
    action_type: str
    handler: Callable      # async (engine, user, params) → dict
    params_schema: type[BaseModel]
    permission: Callable   # (user, params) → bool

class ActionRegistry:
    def register(self, definition: ActionDefinition): ...
    def get(self, action_type: str) -> ActionDefinition | None: ...
    def dispatch(self, action_type: str, params: dict, user: User) -> dict: ...
```

At engine startup `engine_service.initialize()` calls a bootstrap function that registers all known action types. Adding a new action_type = one `registry.register(...)` call.

## Tool API

### `show_modal(title, body, actions)`

LangChain `BaseTool`. Pydantic input schema:
```python
class ShowModalAction(BaseModel):
    label: str
    action_type: str
    params: dict

class ShowModalInput(BaseModel):
    title: str
    body: str
    actions: list[ShowModalAction]
```

Tool logic:
1. Resolve current role from `RunnableConfig.configurable.role_id`.
2. For each action: validate `action.action_type ∈ role.agent_config.allowed_action_types`. If any violates — return error string to LLM ("Action type X is not allowed for this role"), don't emit.
3. Validate `action.params` against the registry's `params_schema` for that action_type. Same error pattern.
4. Return success payload — but **do not write modal to DB here**. The payload is attached to the AI's message during AI message persistence: `ai_service.generate_response` inspects the tool_calls log, finds `show_modal` calls, places their final payload into `messages.metadata.modal`.

Rationale for not writing in the tool body: the AI may call show_modal speculatively and then change its mind in a later step (the LangChain ReAct loop). The final message metadata reflects only the modal that was emitted by the agent's final response, not every speculative one.

### `list_invoices(status, page)` and `get_invoice(invoice_id)`

Standard LangChain tools, similar shape to existing `task_query` / `task_get_own`. Permission filter applied in the tool body using `RunnableConfig.configurable.caller_user_id` + `org_id`.

## UI surfaces

### `/invoices` page

Layout similar to `/tasks`:
- Sidebar group "Счета" alongside Tasks/Projects (item 11 pattern).
- Filter chips: status (`created` / `approved` / `processed` / `rejected` / `all`).
- For admin: extra filter "загрузил" (uploaded_by user picker).
- Table columns: file name (clickable → download), uploaded by, uploaded at, due date, status, actions.
- Action column (admin only, when `status='created'`): Approve / Reject buttons inline — bypasses AI, calls the same action handlers.
- Action column (accountant or admin, when `status='approved'`): "Провести" button → invoice_mark_processed.
- Upload button (top): opens upload modal — file picker + due_date date input. Submit creates user_file + invoice.
- Admin sees a banner at the top: "Бухгалтер для уведомлений: <name>" with dropdown to select / change. Empty state: "Бухгалтер не назначен — уведомления не отправляются. Выбрать".

### Modal renderer (universal)

- Lives in a top-level layout component, listens to WebSocket `message` events.
- On new message with `metadata.modal` → fetches `target` resource's current status → opens overlay modal.
- Modal content: title, body (markdown), action buttons.
- "Открыть файл" link if target is an invoice (links to the user_file download).
- After click: disables all buttons, spinner, calls `POST /api/actions/{action_type}` with `params`. On success: closes modal, success toast. On 409: shows "уже изменено", reloads target.
- For old messages with modal payload on scroll back: same render path — derived state shows whether resolved.

## Scheduler details

`SchedulerService._check_invoice_due_dates(org)`:
1. Compute `today_local` from `org.timezone`.
2. If `org.accountant_user_id IS NULL` → return.
3. Query:
   ```sql
   SELECT i.id, uf.original_filename, uf.summary
   FROM invoices i
   JOIN user_files uf ON uf.id = i.file_id
   WHERE i.org_id = $1
     AND i.status = 'approved'
     AND i.processed_at IS NULL
     AND i.is_active = true
     AND (i.due_date = $2 OR i.due_date = $2 + 1)
     AND (i.last_notified_for_date IS NULL OR i.last_notified_for_date < $2);
   ```
4. For each row: create in_app_notification (type `invoice_due`), then `UPDATE invoices SET last_notified_for_date = $today`. Single transaction per invoice.

`last_notified_for_date DATE` is a new column on invoices added in the same migration to support idempotency without abusing `notification_log`.

## Migration plan

Single migration `043_invoices_and_actions.sql`:
- Create `invoices` table + indexes
- Add `organizations.accountant_user_id` (with FK ON DELETE SET NULL)
- Add `messages.metadata JSONB NOT NULL DEFAULT '{}'` if not already present
- Seed `invoice_clerk` role in system org `00000000-...`: insert into `roles` (org_id=system, code='invoice_clerk', name='Счетовод', prompt_file='invoice_clerk.md', agent_type='simple', agent_config=JSONB with allowed_action_types, tools=JSONB with [list_invoices, get_invoice, show_modal])
- Seed system user `invoice_clerk` (username='invoice_clerk', is_system=true, role_id=above)

Prompt file `src/engine/prompts/invoice_clerk.md` — committed separately (not via migration).

## Risks / open notes

- **RAG-pipeline latency**. Tika + LLM summary may take 10–60s. Worker uploads file, returns immediately; invoice row is created with `summary IS NULL`. AI tools must handle this: if user asks `@@invoice_clerk покажи новые счета` while ingest is still running, AI sees rows but summaries empty. Acceptable for v1; just shows file name. Future: WebSocket notify "summary ready".
- **Frontend modal-renderer placement**. v1 — modal renders only on the active chat page. If user is on another page when AI emits a modal, it surfaces via the existing in-app notification bell (new notification type `modal_pending` pointing to the chat). Implementation lives inside the chat-page component, not at app shell.
- **Mobile UX of modal**. Full-screen modal on a mobile chat already has many overlays (drafts, mention picker). Need explicit z-index discipline. Defer to implementation phase.
- **Audit of accountant changes**. If admin changes `accountant_user_id`, the old accountant's pending notifications stay. Acceptable v1.

## Phases (within v1)

If implementation needs to be sliced for review, this order works:
1. Migration + invoices table + organizations.accountant_user_id + messages.metadata
2. Action registry skeleton + dispatcher endpoint
3. Three invoice action handlers + permission checks
4. `list_invoices` / `get_invoice` tools
5. `show_modal` tool + per-role whitelist enforcement
6. Frontend `/invoices` page (table, upload, direct admin buttons)
7. Frontend modal renderer (WS subscription, overlay, click flow)
8. `invoice_clerk` role + prompt + system user seed + sidebar/picker whitelist update
9. Scheduler job for accountant notifications
10. Bug bash + manual end-to-end test

## What it does NOT lock in

- The G-approach (LangGraph interrupt / checkpointer) remains a viable future evolution. v1 modal payload schema and frontend renderer are reusable: a future graph-based agent can emit the same payload shape through an `interrupt(...)` rather than a tool call, and the renderer doesn't care which mechanism produced it.
- Action registry can grow new action_types without schema changes — only Python code.
- Invoice schema can grow new fields (e.g., extracted amount in v2 once OCR is solid) without modal-protocol changes.
