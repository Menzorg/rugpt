# Раздел «Упоминания»: дизайн

**Дата:** 2026-05-03
**Скоуп:** engine (`/root/rugpt`) + webclient (`/root/webclient_rugpt`)

## Цель

Дать пользователю возможность:

1. Узнавать, что его (`@username`) или его AI-роль (`@@username`) кто-то упомянул в чате — через колокольчик и страницу «Упоминания».
2. Из раздела «Меня» отвечать в исходный чат **не вступая в его участники** — ровно один реплай на одно mentioning-сообщение.
3. Из раздела «Моя роль» (бывшая `MentionReviewList`) валидировать AI-ответы своей роли — **поведение не меняется, только переименование**.

## Текущее состояние (что уже есть)

- `mention_service.py` парсит `@user` и `@@role`, резолвит к user_id, кладёт результат в `messages.mentions`.
- `routes/chats.py:send_message` для каждого `@@role`-mention вызывает `ai_service.process_ai_mentions` → AI генерирует ответ с `ai_validated=false`. Эти ответы попадают в очередь pending-review.
- На фронте `mentions/page.tsx` показывает `MentionReviewList`, который через WS `messages:pending-review` тянет очередь валидаций.
- Колокольчик `useNotifications.ts` дёргает `/api/in-app-notifications/unread-count` каждые **30 сек** (polling).
- Тип `"mention"` объявлен в `valid_types` `in_app_notification_service.py:33` и в DB-схеме миграции 008, **но никто никогда не создаёт нотификацию с этим типом** — это и есть основной баг.
- В `chat_service.send_message` permission-проверки `chat.participants` нет (метод `can_user_access_chat` существует, но в send-пути не вызывается).

## Архитектура

### Потоки данных

**Поток A — `@user`-упоминание:**
1. Пользователь B пишет «`@anna` посмотри это» в чат C.
2. Engine `routes/chats.py:send_message` парсит mentions через `mention_service`.
3. Для каждого `MentionType.USER` (где `mentioned_user_id != sender_id`) engine вызывает `in_app_notification_service.create(user_id=mentioned, type="mention", reference_type="message", reference_id=message.id, title="Вас упомянул @{sender_username}")`.
4. Anna при ближайшем polling-цикле (≤30 сек) видит +1 на колокольчике.
5. Кликнув колокольчик → дропдаун со списком; кликнув строку «Вас упомянул @bob» → `/mentions?tab=me&highlight={notification_id}`.
6. На странице в табе «Меня» Anna видит карточку с текстом исходного сообщения и формой ответа.

**Поток B — `@@role`-упоминание:**
1. B пишет «`@@anna` посмотри договор».
2. Engine идёт **существующим путём** через `ai_service.process_ai_mentions` → AI отвечает, ответ сохраняется с `ai_validated=false`.
3. **Никаких новых in-app нотификаций не создаётся** — Anna узнаёт об этом через очередь валидации.
4. Колокольчик отображает сумму: `unread in_app_notifications + pending validations count`.
5. Кликнув строку валидации → `/mentions?tab=role&highlight={message_id}`.
6. В табе «Моя роль» — текущий `MentionReviewList` без изменений.

**Поток C — Reply на упоминание:**
1. Anna в табе «Меня» жмёт «Ответить» под чьим-то сообщением.
2. Frontend → `POST /api/messages/{message_id}/reply` body `{content}`.
3. Webclient backend проксирует в Engine `POST /api/v1/chats/messages/{message_id}/reply`.
4. Engine: достаёт original message, проверяет «sender упомянут как `@` или его role упомянута как `@@`», проверяет single-use (нет ли уже reply от этого sender'а на это message), вставляет новое message в `original.chat_id` через `chat_service.send_message` с `reply_to_id=message_id`. **`chat.participants` не модифицирует.**
5. Стандартный broadcast в чат — все участники (и Anna в раздел «Меня») видят новое сообщение.

### Безопасность carve-out'а

Reply-on-mention — узкий carve-out. Проверка прав привязана к **конкретному mentioning-сообщению**, а не к чату целиком. Доступ:

- ✅ Можно ответить, если ты в `original.mentions` (неважно через `@` или `@@` — оба типа резолвят к настоящему `user_id` через `mention_service.resolve_mentions`, разница только в `MentionType`)
- ❌ Нельзя ответить произвольно в чат, где тебя нет в `participants`
- ❌ Нельзя ответить дважды на одно mentioning-сообщение (single-use)
- Ответ виден всем в чате через стандартный broadcast — никакого скрытого канала
- Системные AI-роли (`@@pm`, `@@reasoner` и т.п. — фоллбэк-путь резолва на system users) к данной фиче не имеют отношения: за ними нет «пользователя», который мог бы ответить

## Изменения в engine (`/root/rugpt`)

### 1. `src/engine/routes/chats.py`

В обработчике `send_message` после успешного `chat_service.send_message(...)` и **до** `process_ai_mentions`:

```python
sender = await engine.user_storage.get_by_id(user_id)
sender_label = f"@{sender.username}" if sender else "пользователь"
for m in mentions:
    if m.type == MentionType.USER and m.user_id != user_id:
        await engine.in_app_notification_service.create(
            user_id=m.user_id,
            org_id=org_id,
            type="mention",
            title=f"Вас упомянул {sender_label}",
            content=request.content[:200],
            reference_type="message",
            reference_id=message.id,
        )
```

`@@role`-упоминания продолжают идти по существующему пути `process_ai_mentions` — без изменений.

### 2. Новый endpoint `POST /api/v1/chats/messages/{message_id}/reply`

```python
class ReplyToMentionRequest(BaseModel):
    content: str

@router.post("/messages/{message_id}/reply", response_model=MessageResponse)
async def reply_to_mention(
    message_id: UUID,
    request: ReplyToMentionRequest,
    user_id: UUID,
    org_id: UUID,
    engine: EngineService = Depends(get_engine),
):
    original = await engine.chat_service.get_message(message_id)
    if not original:
        raise HTTPException(404, "Message not found")

    sender = await engine.user_storage.get_by_id(user_id)
    if not sender:
        raise HTTPException(404, "User not found")

    if not _is_mentioned(original, sender):
        raise HTTPException(403, "Not mentioned in this message")

    if await engine.message_storage.find_reply(message_id, sender.id):
        raise HTTPException(409, "Already replied to this mention")

    reply = await engine.chat_service.send_message(
        chat_id=original.chat_id,
        sender_id=sender.id,
        content=request.content,
        reply_to_id=message_id,
    )
    return MessageResponse(**reply.to_dict())


def _is_mentioned(original, sender) -> bool:
    """True if sender appears in original.mentions, regardless of mention type.

    `mention_service.resolve_mentions` для обоих `@user` и `@@user` пишет
    реальный `user_id` владельца роли — разница только в `MentionType`.
    Поэтому достаточно простого сравнения user_id, без role_id-логики и
    без обращения к user_storage за дополнительными данными.

    Системные AI-роли типа `@@pm` резолвятся к system user'ам — у них нет
    "хозяина", способного авторизованно вызвать этот endpoint, поэтому
    случай отсекается естественным образом (sender — обычный человек,
    его user_id не совпадёт с system user'овским).
    """
    return any(m.user_id == sender.id for m in (original.mentions or []))
```

### 3. `src/engine/storage/message_storage.py`

Новый метод:

```python
async def find_reply(
    self,
    reply_to_id: UUID,
    sender_id: UUID,
) -> Optional[Message]:
    """Find an existing reply by sender to a specific message. Used for
    single-use mention reply check."""
    row = await self.fetch_one(
        "SELECT * FROM messages WHERE reply_to_id = $1 AND sender_id = $2 LIMIT 1",
        reply_to_id, sender_id,
    )
    return self._row_to_message(row) if row else None
```

### 4. `src/engine/routes/in_app_notifications.py`

Добавить query-параметр `type` для фильтрации:

```python
@router.get("", response_model=List[NotificationResponse])
async def list_notifications(
    user_id: UUID,
    type: Optional[str] = Query(None, description="Filter by notification type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    engine: EngineService = Depends(get_engine),
):
    ...
    notifications = await engine.in_app_notification_service.list_by_user(
        user_id=user_id, type=type, limit=limit, offset=offset, unread_only=unread_only,
    )
    ...
```

### 5. `src/engine/services/in_app_notification_service.py` + `storage/in_app_notification_storage.py`

Расширить `list_by_user` опциональным аргументом `type`. SQL: `WHERE user_id = $1 AND ($2::text IS NULL OR type = $2)`.

### 6. `src/engine/services/in_app_notification_service.py:33`

Уже содержит `"mention"` в `valid_types`. Не трогаем.

## Изменения в webclient backend (`/root/webclient_rugpt/packages/backend`)

### 1. `src/in-app-notification/in-app-notification.controller.ts`

Добавить `@Query('type') type?: string` в `findAll`, прокинуть в сервис.

### 2. `src/in-app-notification/in-app-notification.service.ts`

Принять `type` в payload, передать в адаптер.

### 3. `src/engine/adapters/rugpt.adapter.ts` — case `get_in_app_notifications`

Добавить:
```typescript
if (payload.type) params.append('type', payload.type);
```

### 4. `src/chat/chat.controller.ts`

Новый endpoint:
```typescript
@Post('messages/:id/reply')
async replyToMention(
  @Param('id') messageId: string,
  @Body() body: { content: string },
  @Request() req: any,
) {
  return this.chatService.replyToMention(messageId, body.content, req.user);
}
```

### 5. `src/chat/chat.service.ts`

Новый метод:
```typescript
async replyToMention(messageId: string, content: string, currentUser: CurrentUser) {
  const [success, data] = await this.engineAdapter.execute('reply_to_mention', {
    message_id: messageId,
    content,
    token: currentUser.engineToken,
  });
  if (!success) throw new Error(typeof data === 'string' ? data : 'Failed to reply');
  return data;
}
```

### 6. `src/engine/adapters/rugpt.adapter.ts` — новый case

```typescript
case 'reply_to_mention':
  return this.request(
    'POST',
    `/api/v1/chats/messages/${encodeURIComponent(payload.message_id)}/reply`,
    { content: payload.content },
    headers,
  );
```

## Изменения во frontend (`/root/webclient_rugpt/packages/frontend`)

### 1. Новый хук `src/app/hooks/useMyMentions.ts`

Полностью аналогичен `useNotifications.ts`, но фильтрует по `type=mention`. Также экспортирует `replyToMention(messageId, content)`.

```typescript
export function useMyMentions() {
  const [mentions, setMentions] = useState<InAppNotification[]>([]);
  const [loading, setLoading] = useState(true);
  const token = useAuthStore(s => s.token);
  const user = useAuthStore(s => s.user);

  const fetch = useCallback(async () => {
    if (!token) { setLoading(false); return; }
    try {
      const api = getApiClient();
      const data = await api.signedGet<InAppNotification[]>(
        '/api/in-app-notifications?type=mention&limit=50', user?.id,
      );
      setMentions(data || []);
    } finally {
      setLoading(false);
    }
  }, [token, user?.id]);

  const replyToMention = useCallback(async (messageId: string, content: string) => {
    if (!token) throw new Error('Not authenticated');
    const api = getApiClient();
    return api.signedPost(`/api/messages/${messageId}/reply`, { content }, user?.id);
  }, [token, user?.id]);

  useEffect(() => { fetch(); }, [fetch]);
  useEffect(() => {
    if (!token) return;
    const id = setInterval(fetch, 30000);
    return () => clearInterval(id);
  }, [token, fetch]);

  return { mentions, loading, refetch: fetch, replyToMention };
}
```

### 2. Новый компонент `src/app/components/MyMentionsList.tsx`

Принимает `currentUserId` и `users` (для резолва имён). Для каждой нотификации:
- Загружает `original message` через `signedGet('/api/messages/${reference_id}')` (пер-row, lazy)
- Рисует карточку: «`{senderName}` `{originalText}` `{timeAgo}`»
- Inline textarea + кнопка «Ответить»
- При успехе → карточка превращается в `{originalText}` + «✓ Отвечено: `{replyText}`», кнопки нет
- На 409 → «Уже отвечено», disable формы
- На 403 → «Нет доступа», disable формы

### 3. `src/app/mentions/page.tsx` — рефактор с табами

```tsx
function MentionsPageInner() {
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<'me' | 'role'>(
    searchParams.get('tab') === 'role' ? 'role' : 'me'
  );
  const { mentions } = useMyMentions();
  const { messages: pendingValidations } = useMentionReview();
  const myUnread = mentions.filter(n => !n.is_read).length;

  return (
    <Suspense ...>
      <Sidebar ... />
      <ChatNavigation title="Упоминания" />
      <Tabs>
        <Tab active={tab === 'me'} onClick={() => setTab('me')}>
          Меня {myUnread > 0 && <Badge>{myUnread}</Badge>}
        </Tab>
        <Tab active={tab === 'role'} onClick={() => setTab('role')}>
          Моя роль {pendingValidations.length > 0 && <Badge>{pendingValidations.length}</Badge>}
        </Tab>
      </Tabs>
      {tab === 'me' ? <MyMentionsList ... /> : <MentionReviewList ... />}
    </Suspense>
  );
}
```

URL-параметры: `?tab=me|role`, `?highlight={id}`. Подсветка использует существующий `useEffect` с `tryHighlight`.

### 4. `src/app/hooks/useNotifications.ts` — bell counter

Расширить `unreadCount`:
```ts
const { count: pendingValidationCount } = usePendingValidationCount(); // новый легковесный хук
const totalCount = unreadCount + pendingValidationCount;
return { ..., unreadCount: totalCount };
```

`usePendingValidationCount` — новый, тонкая обёртка над WS `messages:pending-review` либо новый light REST endpoint `/api/messages/pending-review-count`. Решение: WS, чтобы не плодить REST.

### 5. `src/app/components/NotificationDropdown.tsx`

Изменить routing для `type === 'mention'`:
```ts
if (n.type === 'mention') return `/mentions?tab=me&highlight=${n.id}`;
```

Дропдаун рендерит **только** записи из `in_app_notifications` (как сейчас). Валидации AI-ответов в нём НЕ показываем — попасть в них можно только через `/mentions?tab=role`. Колокольчик-каунтер при этом всё равно учитывает обе суммы — то есть число на колокольчике может быть больше, чем число строк в дропдауне; в дропдауне внизу будет ссылка «Открыть упоминания» для перехода на полную страницу.

## Что НЕ делаем (явный non-scope)

- WebSocket-пуш для in-app нотификаций. Только polling, как сейчас.
- Telegram/Email уведомления для упоминаний. Эти каналы в проде не настроены.
- Изменение DB-схемы. Используем существующие таблицы.
- Безусловное добавление в `chat.participants` при reply.
- Множественные реплаи на одно mentioning-сообщение.
- Поведение текущего `MentionReviewList` (валидация AI-ответов). Только переименование таба.
- Уникальный индекс в БД на `(reply_to_id, sender_id)`. Защита через SELECT-перед-INSERT в коде (потенциальная гонка считается приемлемой — двойной reply почти невозможен в реальном UI).

## Критерии приёмки

1. Пользователь A пишет «`@anna` привет» в чат, где Anna не участник. Anna в течение 30 сек видит +1 на колокольчике.
2. Anna открывает `/mentions`, таб «Меня» автоселектен, в списке есть строка с текстом «привет», именем A, временем.
3. Anna пишет реплай «ок, посмотрю» и жмёт «Ответить». Сообщение появляется в исходном чате, видно всем участникам, у Anna карточка превращается в «✓ Отвечено».
4. Попытка ответить второй раз на то же mentioning-сообщение возвращает 409, кнопка disable.
5. Попытка вызвать `POST /api/messages/{id}/reply` от пользователя, не упомянутого в этом message, возвращает 403.
6. Пользователь B пишет «`@@anna` посмотри договор». AI генерирует ответ. Anna видит +1 на колокольчике (от pending-validation), таб «Моя роль» содержит карточку валидации (текущее поведение).
7. Бэйдж колокольчика в сайдбаре равен сумме unread mention-нотификаций + pending-validations.

## Тестирование

- Engine: unit для `_is_mentioned`, `find_reply`, integration для нового endpoint (200 OK при mention, 403 без mention, 409 при повторе)
- Webclient backend: unit для адаптера (правильный URL/body), e2e для proxy-роута
- Frontend: ручная проверка по сценарию (1)–(7)

## Открытые вопросы (на момент написания спеки нет)

Все ключевые развилки приняты пользователем в ходе brainstorm-сессии 2026-05-03.
