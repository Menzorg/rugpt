# Chat Unread Status — Design Spec

**Дата:** 2026-05-05
**Автор:** brainstorming (rugpt)
**Статус:** утверждено пользователем для перехода к плану

## Цель

Внедрить в rugpt систему read/unread статуса сообщений: показывать счётчик непрочитанных сообщений возле каждого чата (sidebar direct/task/project, tasks list, projects list).

## Семантика

- **High-water-mark per (chat, user).** На каждую пару `(chat_id, user_id)` хранится `last_read_message_id` и `last_read_at`. Unread = сообщения в чате созданные позже `last_read_at`, кроме своих.
- **Mark-as-read срабатывает на open чата.** Один раз при mount страницы и каждый раз когда приходит новое входящее в открытый чат. Никаких scroll-tracking, IntersectionObserver, visibility checks.
- **Что попадает в счётчик:** все сообщения кроме отправленных самим юзером. AI-ответы и system-сообщения тоже считаются — фильтр только по `sender_id != current_user`. Soft-deleted (`is_deleted=true`) исключаются.
- **Counter cap = 100.** UI показывает `99+` для значений ≥ 99.
- **Только my-unread.** Read-marker отправителю не показываем (никаких "прочитано"-галочек). MVP-scope — счётчики в sidebar/lists.

## Где показывать badges

1. **Sidebar direct-чаты** — возле аватара каждого юзера (по образцу support-icon `Sidebar.tsx:311-318`).
2. **Sidebar task/project chats** — возле названия задачи/проекта в `SidebarTaskProjectSections`.
3. **Tasks list** (`/tasks`) — на строке задачи рядом с заголовком.
4. **Projects list** (`/projects`) — на строке проекта.

В системные direct-чаты с `is_system=true` юзерами badge не отрисовывается, потому что они скрыты из sidebar (per memory "System users hidden").

## Архитектура

### Поток данных

```
A пишет → Engine INSERT messages → Kafka chat.events → NestJS broadcast → B's WS receives
B's frontend → useChatUnreadBootstrap.messages$ → increment(chatId) → badge "+1" в sidebar
B открывает чат → useChatService mount effect → markAsRead(last_msg_id) → WS MESSAGE_READ
NestJS handleMessageRead → POST /chats/{id}/read → Engine UPSERT chat_read_state
NestJS → emit chat:unread-cleared в user:${B.id} → все вкладки B обнуляют локальный counter
```

### Слои

- **Engine**: 1 миграция + 1 storage класс + 2 service метода + 2 endpoints
- **NestJS backend**: 2 case'а в adapter + 2 метода в ChatService + 1 HTTP endpoint + замена no-op WS-handler + 1 новый WS server-event
- **Frontend**: 1 zustand store + 1 bootstrap-хук + 1 effect в useChatService + 4 точки UI-интеграции (Sidebar direct rows, Sidebar task/project sections, tasks list, projects list)

## Engine

### Миграция `019_chat_read_state.sql`

```sql
CREATE TABLE chat_read_state (
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    last_read_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX idx_chat_read_state_user ON chat_read_state(user_id);
```

**Обоснование решений:**

- `last_read_at TIMESTAMPTZ NOT NULL` — основа подсчёта. Существующий индекс `messages(chat_id, created_at)` (`001_initial.sql:115`) делает COUNT через timestamp быстрым.
- `last_read_message_id UUID` — справочно, для будущей фичи галочек "прочитано" и для мониторинга. `ON DELETE SET NULL` — если сообщение soft-удалят жёстко, не теряем читательскую позицию.
- `PRIMARY KEY (chat_id, user_id)` — естественный композит, идемпотентный UPSERT.
- `idx_chat_read_state_user` — для bulk-запроса unread-counts по всем чатам юзера за один SQL.

**Что НЕ добавляется:** денормализованный `unread_count` (поддерживать инкрементами при INSERT messages — N writes на отправку и race conditions; COUNT через индекс достаточно). Триггеры. Отдельный JSONB-колумн с прочитанностью.

### Storage `src/engine/storage/chat_read_state_storage.py`

```python
class ChatReadStateStorage:
    async def upsert(self, chat_id: UUID, user_id: UUID,
                     last_read_message_id: UUID, last_read_at: datetime) -> None:
        """
        UPSERT с monotonic guard — не двигаем high-water-mark назад.
        SQL:
          INSERT INTO chat_read_state (chat_id, user_id, last_read_message_id, last_read_at)
          VALUES ($1, $2, $3, $4)
          ON CONFLICT (chat_id, user_id) DO UPDATE
          SET last_read_message_id = EXCLUDED.last_read_message_id,
              last_read_at = EXCLUDED.last_read_at,
              updated_at = NOW()
          WHERE EXCLUDED.last_read_at >= chat_read_state.last_read_at
        """

    async def get_unread_counts_for_user(self, user_id: UUID, org_id: UUID
                                         ) -> dict[UUID, int]:
        """
        Один SQL для всех чатов юзера в org. Возвращает {chat_id: count}, только для count > 0.
        SQL:
          SELECT c.id AS chat_id,
                 LEAST(COUNT(m.id), 100) AS unread
          FROM chats c
          LEFT JOIN chat_read_state crs ON crs.chat_id = c.id AND crs.user_id = $1
          LEFT JOIN messages m
            ON m.chat_id = c.id
            AND m.sender_id != $1
            AND m.is_deleted = false
            AND m.created_at > COALESCE(crs.last_read_at, '-infinity'::timestamptz)
          WHERE c.org_id = $2
            AND c.is_active = true
            AND $1 = ANY(c.participants)
          GROUP BY c.id
          HAVING COUNT(m.id) > 0
        """
```

### Service `chat_service.py` — два новых метода

```python
async def mark_chat_read(self, chat_id: UUID, user_id: UUID, message_id: UUID) -> None:
    msg = await self.message_storage.get_by_id(message_id)
    if not msg or msg.chat_id != chat_id:
        raise NotFound("Message not in chat")
    chat = await self.chat_storage.get_by_id(chat_id)
    if user_id not in chat.participants:
        raise Forbidden("Not a participant")
    await self.chat_read_state_storage.upsert(chat_id, user_id, msg.id, msg.created_at)

async def list_unread_counts(self, user_id: UUID, org_id: UUID) -> dict[UUID, int]:
    return await self.chat_read_state_storage.get_unread_counts_for_user(user_id, org_id)
```

### Endpoints `routes/chats.py`

```
POST /api/v1/chats/{chat_id}/read?user_id=...
  body: { "message_id": "uuid" }
  → 204 No Content
  Errors: 404 если message не в этом чате, 403 если user не участник

GET /api/v1/chats/unread-counts?user_id=...
  → 200 { "chat-uuid-1": 5, "chat-uuid-2": 12, ... }
  Возвращает только чаты с count > 0
```

Расширение `ChatResponse.unread_count` НЕ делается — отдельный endpoint независим от тяжёлого `/chats/my`, кэш-стратегии разные.

## NestJS backend

### Adapter `engine/adapters/rugpt.adapter.ts`

Два новых case'а в `execute()`:

```typescript
case 'mark_chat_read':
  return this.client.post(
    `/api/v1/chats/${payload.chatId}/read?user_id=${payload.userId}`,
    { message_id: payload.messageId },
    correlationId,
  );

case 'get_unread_counts':
  return this.client.get(
    `/api/v1/chats/unread-counts?user_id=${payload.userId}`,
    correlationId,
  );
```

### ChatService `chat/chat.service.ts`

```typescript
async markChatRead(chatId: string, userId: string, messageId: string,
                   correlationId: string): Promise<void> {
  await this.engineAdapter.execute('mark_chat_read',
    { chatId, userId, messageId }, correlationId);
}

async getUnreadCounts(userId: string,
                      correlationId: string): Promise<Record<string, number>> {
  return this.engineAdapter.execute('get_unread_counts',
    { userId }, correlationId);
}
```

### HTTP endpoint `chat/chat.controller.ts`

```typescript
@Get('unread-counts')
@UseGuards(JwtAuthGuard)
async getUnreadCounts(@Req() req: AuthenticatedRequest) {
  return this.chatService.getUnreadCounts(req.user.userId, req.correlationId);
}
```

Эндпоинт `POST /chat/{id}/read` НЕ нужен — фронт ходит через WS event `MESSAGE_READ`, который уже определён в shared types.

### WebSocket gateway `socket/socket.gateway.ts:422-452`

Замена существующего no-op skeleton'а:

```typescript
@SubscribeMessage(WsClientEvents.MESSAGE_READ)
async handleMessageRead(
  @MessageBody() payload: MessageReadPayload,
  @ConnectedSocket() client: AuthenticatedSocket,
) {
  const userId = client.userId;
  if (!userId) return { success: false, error: 'Unauthorized' };

  // Skip optimistic temp-ids — Engine не знает таких сообщений
  if (payload.messageId.startsWith('temp-')) return { success: true };

  // Validate participant
  const chat = await this.chatService.getChat(payload.chatId, userId);
  if (!chat || !chat.participants.includes(userId)) {
    return { success: false, error: 'Not a participant' };
  }

  await this.chatService.markChatRead(payload.chatId, userId, payload.messageId,
                                       client.correlationId);

  // Multi-device sync — все вкладки/устройства того же юзера обнулят локальный counter
  this.io.to(`user:${userId}`).emit('chat:unread-cleared', { chatId: payload.chatId });

  return { success: true };
}
```

### Новое WS server-event

В `packages/common/src/websocket/events.ts` добавить в `WsServerEvents`:

```typescript
CHAT_UNREAD_CLEARED = 'chat:unread-cleared',
```

С payload `{ chatId: string }`. Никаких других новых WS-событий — broadcast read-marker другим участникам НЕ делаем.

## Frontend

### Zustand store `app/hooks/useChatUnreadStore.ts` (новый)

```typescript
interface ChatUnreadState {
  counts: Record<string, number>;
  hasFetchedOnce: boolean;
  setAll: (counts: Record<string, number>) => void;
  setCount: (chatId: string, count: number) => void;
  increment: (chatId: string) => void;
  clear: (chatId: string) => void;
  totalForChats: (chatIds: string[]) => number;
}
```

Без persist. На reload счётчики восстанавливаются из API.

### Bootstrap-хук `app/hooks/useChatUnreadBootstrap.ts` (новый)

Вызывается один раз в `Sidebar.tsx` (он на всех страницах с чатами через layout). Подписки:

1. **Initial fetch** — `signedGet('/api/chat/unread-counts')` → `setAll(counts)`
2. **WS messages$** — для каждого нового сообщения от другого юзера → `increment(chatId)`
3. **WS connectionState$ === 'connected'** — после reconnect перезапросить `/unread-counts` (offline-догон)
4. **WS chatUnreadCleared$** (новая подписка на `CHAT_UNREAD_CLEARED`) — `clear(chatId)` (multi-device sync)

### Mark-as-read в `useChatService.ts`

Дополнительный effect (после mount-effect на строках 93-260):

```typescript
useEffect(() => {
  if (!chatId || !messages.length || !currentUserId) return;
  const lastMsg = messages[messages.length - 1];
  if (lastMsg.id.startsWith('temp-')) return;       // skip optimistic
  if (lastMsg.senderId === currentUserId) return;    // own — нечего читать

  service.markAsRead(lastMsg.id);
  useChatUnreadStore.getState().clear(chatId);       // optimistic local
}, [chatId, messages, currentUserId]);
```

`service.markAsRead` уже существует (`useChatService.ts:320-322` → `chatService.ts:132-134`), эмитит WS `MESSAGE_READ`.

### Resolver peerUserId → chatId для sidebar

Sidebar рендерит direct-row по **юзеру** (а не по чату). Нужен mapping `peerUserId → chatId`, чтобы найти counter в store.

В sidebar используется хук, который получает direct-чаты из `/chats/my`. Каждый direct-чат имеет `participants: [user_a, user_b]`. На frontend:

```typescript
const peerToChat: Record<string, string> = useMemo(() => {
  const map: Record<string, string> = {};
  for (const c of directChats) {
    if (c.type !== 'direct') continue;
    const peer = c.participants.find((id) => id !== currentUserId);
    if (peer) map[peer] = c.id;
  }
  return map;
}, [directChats, currentUserId]);
```

Если direct-чата у пары не существует (никто не открывал страницу `/chat/{username}`) — ключа в Map нет, badge не рисуется. Это корректно: нет чата → нет сообщений → нет unread.

### UI integration

1. **Sidebar direct rows** — `Sidebar.tsx` `renderRow` (~359-401): расширить `SidebarChatItem` опциональным `unreadCount?: number`, проставлять через `peerToChat[user.id]` → `unreadStore.counts[chatId]`. Рендер badge по образцу support-icon `:311-318`.
2. **Sidebar task/project sections** — `SidebarTaskProjectSections` (~22-109): передавать `unreadCount` для каждой строки (там уже есть `chatId`).
3. **Tasks list `tasks/page.tsx`** — на строке задачи: `useChatUnreadStore((s) => s.counts[task.chatId] ?? 0)`. Badge возле заголовка.
4. **Projects list `projects/page.tsx`** — то же для `project.chatId`.

## Edge cases

1. **Multi-device одного юзера** — после `markChatRead` backend эмитит `chat:unread-cleared` в `user:${userId}` room → все вкладки/устройства обнуляют локальный counter без обращения к Engine.
2. **Race: новое сообщение во время mark-read** — UPSERT'ит `last_read_at = lastSeen.created_at`. Новое сообщение остаётся unread (как и должно быть). WS приносит его, useChatService effect срабатывает на обновлённый `messages`, отправляет новый mark-read.
3. **Concurrent multi-tab** — `WHERE EXCLUDED.last_read_at >= chat_read_state.last_read_at` гарантирует, что high-water-mark не двигается назад.
4. **Optimistic temp-id** — early return на frontend и backend; своё сообщение mark-read'ить не нужно (после ack от engine оно сменит ID на реальный, effect отработает на настоящий).
5. **System-user direct chat** — system-юзеры скрыты из sidebar, badge не рисуется. Engine считает unread для этого чата как обычно — если в будущем понадобится отдельная страница "AI Assistant", counter уже есть.
6. **Soft-deleted сообщения** — SQL `count_unread`: `AND is_deleted = false`.
7. **Удаление из чата** — read-state record остаётся orphan (нет FK на participants array), но через `/chats/my` чат не возвращается → unread не считается. Не критично.
8. **Offline догон** — `wsClient.connectionState$` подписан в bootstrap-хуке, на каждый reconnect re-fetch `/unread-counts`.
9. **Cap = 100** — SQL `LEAST(COUNT(*), 100)`. UI отображает `99+` для значений ≥ 99.
10. **Latency на старте сессии** — до первого ответа `/unread-counts` (~100ms) badges не рисуются. Никаких "Loading…" в sidebar.

## Тестирование

### Engine (pytest)

`tests/storage/test_chat_read_state_storage.py`:
- `test_upsert_creates_row`
- `test_upsert_monotonic_guard` (старый last_read_at не двигает HWM назад)
- `test_get_unread_counts_excludes_own`
- `test_get_unread_counts_excludes_deleted`
- `test_get_unread_counts_caps_at_100`
- `test_get_unread_counts_skips_zero_chats`

`tests/services/test_chat_service_read.py`:
- `test_mark_chat_read_validates_participant` (403 для не-участника)
- `test_mark_chat_read_validates_message_in_chat` (404 если message из другого chat'а)

`tests/routes/test_chats_read.py`:
- `test_post_read_204` (happy path)
- `test_get_unread_counts_returns_dict` (формат ответа)
- `test_get_unread_counts_only_my_chats` (изоляция)

### Backend (Jest)

`packages/backend/src/socket/__tests__/socket.read.spec.ts`:
- `test_handle_message_read_validates_participant`
- `test_handle_message_read_skips_temp_id`
- `test_handle_message_read_emits_unread_cleared_to_user_room`

`packages/backend/src/chat/__tests__/chat.controller.spec.ts`:
- `test_get_unread_counts_endpoint`

Существующий тест `socket.chat.spec.ts:708` `"should update unread messages status when joining chat"` переработать под новую логику (сейчас он валидирует no-op skeleton).

### Frontend (Vitest)

`app/hooks/useChatUnreadStore.test.ts`:
- `setAll`, `setCount`, `increment`, `clear`, `totalForChats`

`app/hooks/useChatUnreadBootstrap.test.tsx`:
- На mount → fetch unread-counts → store обновлён
- WS message от другого юзера → `increment(chatId)`
- WS message от себя → counter не растёт
- WS reconnect → re-fetch
- `chatUnreadCleared$` → `clear(chatId)`

`domain/chat/useChatService.test.ts` (расширить):
- `test_marks_read_on_mount_with_messages`
- `test_skips_temp_id_in_mark_read`
- `test_skips_own_message_in_mark_read`

### E2E ручная проверка

Сценарии:
1. A пишет B (B офлайн) → A не имеет unread у себя. B заходит → видит badge "1" возле A в sidebar → открывает чат → badge исчезает мгновенно.
2. B держит чат открытым, A пишет 5 раз → у B badge не появляется (effect mark-read срабатывает на каждое новое).
3. B открыл чат на двух вкладках → читает на одной → на второй badge тоже обнуляется (multi-device sync через `chat:unread-cleared`).
4. B reload страницы → badge восстанавливается из `/unread-counts`.
5. Tasks list: задача со свежими сообщениями → badge на строке. Открывает чат → возвращается → badge ушёл.
6. Projects list: то же.

### Out of scope

- Performance: 10k чатов в org, 100k сообщений на чат. Если будет проблема — оптимизируем по факту, индексы и cap есть.
- Broadcast read-marker отправителю с галочками "прочитано" — не делаем в этом MVP. Может быть добавлено отдельной фичей: shared types `MessageDeliveryStatus.READ` уже есть, инфра не блокирует.

## Что НЕ делаем (явно зафиксированный YAGNI)

- Никакого WS-broadcast `message:read` другим участникам, никаких галочек "прочитано" у отправителя.
- Не нормализуем `chats.participants TEXT[]` в junction-таблицу `chat_participants`. Read-state живёт в отдельной таблице. Когда понадобится второе per-participant поле (mute, notification settings) — нормализация будет отдельной задачей.
- AI и system-сообщения не выделяются отдельным счётчиком — общий unread.
- Mark-read не привязан к scroll/visibility — только к open чата (mount + новые входящие в открытый чат).
- Не расширяем `/chats/my` полем `unread_count` — отдельный endpoint, разные кэш-стратегии.
- Не делаем UI "Loading badges…" пока счётчики ещё не пришли — просто не рисуем до фетча.
- Не делаем периодическую очистку orphan записей в `chat_read_state` — мусор не критичный.
