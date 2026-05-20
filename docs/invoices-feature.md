# Счета (invoices) + универсальный модальный протокол

**Дата:** 2026-05-20
**Статус:** код реализован и поюнитно протестирован; end-to-end с живым инференсом + браузером и деплой на прод — не делались.
**Spec:** `docs/superpowers/specs/2026-05-19-invoice-flow-and-modal-protocol.md`
**Планы:** `docs/superpowers/plans/2026-05-19-{modal-protocol-infra,invoices-module,invoice-clerk-and-scheduler}.md`

## Зачем

Боль руководителя: сотрудники присылают счета, он должен валидировать каждый (принять / отклонить), а бухгалтер в день оплаты должен получать напоминание «провести счёт».

Архитектурный принцип: **AI-роль не пишет данные напрямую** (защита от галлюцинаций LLM) — только читает и готовит решение. Само изменение данных инициирует человек кликом по кнопке в модальном окне, в обход AI.

Решение разбито на 3 независимых слоя:

1. **Модальный протокол** — универсальный примитив «AI готовит карточку → человек кликает → backend меняет данные». Generic, переиспользуем для любых «AI предлагает, человек решает» сценариев (одобрение тендера, согласование договора, закрытие задачи и т.д.).
2. **Модуль счетов** — сущность `invoices`, заливка, валидация, статусы. Работает и без AI (прямые кнопки в UI).
3. **AI-роль «Счетовод» + scheduler** — роль читает счета и предлагает модалки; scheduler шлёт бухгалтеру напоминания по `due_date`.

## Поток в действии

```
Worker → /invoices «Загрузить» → invoice (status=created) + RAG-summary файла
Руководитель → @@invoice_clerk «покажи счета на проверке» → list_invoices → список
            → «утверди счёт <id>» → get_invoice + show_modal → overlay с кнопками
            → клик «Утвердить» → POST /api/actions/invoice_approve → status=approved
Scheduler (за день и в день due_date) → in-app уведомление назначенному бухгалтеру
Бухгалтер → /invoices «Провести» → status=processed
```

Status flow: `created → approved | rejected`; `approved → processed`.

## Как устроен модальный протокол (T-подход, tool-based)

- AI вызывает read-only tool `show_modal(title, body, actions=[{label, action_type, params}])`. Tool ничего не пишет — только эмитит payload.
- Payload оседает в `messages.metadata.modal` (JSONB) на AI-сообщении. Отдельной таблицы `modal_requests` нет — состояние модалки выводится из состояния target-ресурса (например `invoices.status`).
- `action_type_registry` — Python-словарь на engine: `{action_type → handler + params_schema + permission}`. Единый чокпоинт для авторизации/валидации.
- Frontend `ChatModalOverlay` подписан на сообщения, при наличии `metadata.modal` рендерит overlay. Клик по кнопке → `POST /api/v1/actions/{action_type}` → dispatcher → handler меняет данные.
- Защита от prompt-injection двумя слоями: (1) `role.agent_config.allowed_action_types` whitelist — роль может эмитить только разрешённые типы; (2) `permission(user, params)` на handler'е.

> Будущая эволюция (отложено): LangGraph-interrupt подход («G»), где граф замораживается на взаимодействии с пользователем и продолжается после ответа. Card-схема и frontend-renderer переиспользуемы — поменяется только механизм инициации.

## Файлы

### Engine (`/root/rugpt`)

**Миграции (новые):**
- `043_messages_metadata.sql` — `messages.metadata JSONB` для payload модалки
- `044_invoices.sql` — таблица `invoices` + `organizations.accountant_user_id` + индексы
- `045_invoice_clerk_and_notify_tracker.sql` — роль + system user `invoice_clerk` в системной org `00000000-...` + `invoices.last_notified_for_date`

**Модальный протокол (новое):**
- `actions/registry.py` — `ActionRegistry`, `ActionDefinition`, ошибки (Unknown/PermissionDenied/ActionError); `dispatch()` валидирует params + permission
- `actions/bootstrap.py` — `register_all()` регистрирует production-action'ы при старте
- `actions/invoice_actions.py` — handler'ы `invoice_approve` / `invoice_reject` / `invoice_mark_processed`
- `routes/actions.py` — `POST /api/v1/actions/{action_type}` (404/403/400 на unknown/denied/bad-params)
- `agents/tools/show_modal.py` — read-only tool, per-role whitelist, эмитит `<<MODAL_EMITTED>>{json}<</MODAL_EMITTED>>`

**Модуль счетов (новое):**
- `models/invoice.py` — `Invoice` + `InvoiceStatus`
- `storage/invoice_storage.py` — CRUD + `list_by_org` / `list_for_user` (permission-scoped) + scheduler-query
- `services/invoice_service.py` — `upload` (+ `file_service.index_for_rag` для summary) + `approve/reject/mark_processed` с guard'ами переходов
- `routes/invoices.py` — `POST/GET /invoices`, `GET /invoices/{id}`, tenant-scoped (admin — все, юзер — свои)

**AI-роль (новое):**
- `prompts/invoice_clerk.md` — промпт роли Счетовод
- `agents/tools/list_invoices.py` — read-only список (caller-scoped)
- `agents/tools/get_invoice.py` — read-only детали одного счёта + summary

**Тесты (новые, ~40):** `test_action_registry`, `test_action_route`, `test_show_modal_tool`, `test_ai_service_modal_payload`, `test_invoice_storage`, `test_invoice_service`, `test_invoice_actions`, `test_invoice_routes`, `test_org_routes_accountant`, `test_list_invoices_tool`, `test_get_invoice_tool`, `test_scheduler_invoice_due`

**Модифицированы:**
- `app.py` — подключены `actions_router` + `invoices_router`
- `routes/__init__.py` — экспорт новых router'ов
- `services/engine_service.py` — `action_registry` + bootstrap; регистрация tools (show_modal/list_invoices/get_invoice); `invoice_storage`/`invoice_service`; проводка scheduler-deps
- `services/ai_service.py` — извлечение `<<MODAL_EMITTED>>` из tool-calls → `messages.metadata.modal`
- `agents/executor.py` — прокидка `role` в `RunnableConfig.configurable` (чтобы show_modal видел whitelist)
- `models/message.py` — поле `metadata: dict`
- `storage/message_storage.py` — read/write `metadata`
- `models/organization.py` — поле `accountant_user_id`
- `storage/org_storage.py`, `services/org_service.py`, `routes/organizations.py` — проводка `accountant_user_id` + PATCH принимает его
- `services/scheduler_service.py` — daily job `_notify_invoice_due` (за день/в день, idempotency через `last_notified_for_date`)
- `services/in_app_notification_service.py` — тип `invoice_due` в `valid_types`

### Webclient (`/root/webclient_rugpt`)

**Backend (NestJS) — новое:**
- `action/` (module/controller/service) — прокся `POST /api/actions/:type` → engine
- `invoice/` (module/controller/service) — прокся `/api/invoices/*` → engine

**Backend — модифицировано:**
- `engine/adapters/rugpt.adapter.ts` — cases `dispatch_action`, `list_invoices`, `get_invoice`, `upload_invoice` + `accountant_user_id` в `update_organization`
- `app.module.ts` — регистрация ActionModule + InvoiceModule
- `organization/{service,controller}.ts` — `accountantUserId` в org-update + mapOrg

**Common:**
- `common/src/types/invoice.ts` (новый) — `Invoice` / `InvoiceStatus` (shared FE+BE)
- `common/src/index.ts` — реэкспорт

**Frontend (Next.js) — новое:**
- `components/ModalRenderer.tsx` — overlay с кнопками
- `components/ChatModalOverlay.tsx` — drop-in widget, ищет последнюю unresolved модалку в `messages`
- `hooks/useModalAction.ts` — POST в `/api/actions/*`
- `invoices/page.tsx` — таблица счетов + фильтр статусов + admin-кнопки approve/reject/провести
- `invoices/UploadInvoiceModal.tsx` — заливка файла + due_date
- `invoices/AccountantSelector.tsx` — admin выбирает бухгалтера для уведомлений
- `hooks/useInvoices.ts` — список счетов

**Frontend — модифицировано:**
- `chat/[username]/page.tsx` — mount `<ChatModalOverlay>` (direct-chat со Счетоводом). Остальные чат-страницы получат widget при ChatShell-refactor (см. `webclient_rugpt/doc/TECH_DEBT.md` «Унификация чат-страниц»)
- `components/Sidebar.tsx` — группа «ИИ» whitelist `['pm','invoice_clerk']` + nav-кнопка «Счета»
- `components/ChatInput.tsx` — invoice_clerk первым в @@-picker'е

## Права доступа

| Операция | Кто |
|---|---|
| Upload (`POST /invoices`) | любой active юзер в org |
| Список/детали своих | любой — фильтр `uploaded_by_user_id = self` |
| Список/детали всех в org | только admin |
| `invoice_approve` / `invoice_reject` | только admin |
| `invoice_mark_processed` | `org.accountant_user_id` или admin |
| Назначить бухгалтера (PATCH org) | только admin |

## Технические заметки и подводные камни (пойманы при реализации)

- **`tokenizers`** не был установлен в venv — поставлен (`pip install 'tokenizers>=0.21.0'`), иначе engine не импортируется.
- **`agent_type` invoice_clerk = `simple`**, не `supervisor`. Supervisor — для мультиагентной оркестрации с сабагентами; invoice_clerk — single ReAct-агент с tools.
- **Config-инжекция в LangChain tools:** параметр должен быть `config: RunnableConfig` БЕЗ дефолтного значения и ПЕРЕД defaulted-аргументами. `config: RunnableConfig = None` или `Annotated[..., InjectedToolArg]` НЕ инжектятся (приходит None) в langchain-core 1.3.0.
- **`@SkipSignature()`** обязателен на invoice-upload (multipart через plain fetch, без подписи) — legacy-паттерн как у `file.controller.ts`.
- **`@webchat/common`** надо пересобирать (`npm run build` в packages/common) после добавления нового типа — иначе dist устаревший.
- **RAG-индексация** не запускается автоматически при `file_service.upload` — `InvoiceService.upload` явно зовёт `index_for_rag` (best-effort, не блокирует создание счёта).
- **Тесты с EngineService-синглтоном** требуют `loop_scope="module"` (asyncpg-пул живёт на event loop первого теста). На изоляции зелёные; в full-suite — pre-existing event-loop-scope шум между модулями.
- **`invoice_due`** добавлен в `InAppNotificationService.valid_types` — иначе `create()` бросает ValueError.

## Что НЕ сделано

- End-to-end manual smoke (Task 8 третьего плана): рестарт engine + браузер + логины под разными юзерами + живой инференс gemma → show_modal → overlay → клик. Чеклист — в плане invoice-clerk-and-scheduler, Task 8.
- Деплой на прод (миграции 043/044/045 + рестарт engine + rebuild webclient docker).
- TZ-фикс (option B) для дедлайнов — отдельная отложенная задача, не связана со счетами напрямую.
