# RuGPT LLM Integration

Интеграция с Large Language Models.

## Архитектура

```
ChatService
    │
    └── LLMProvider
            │
            ├── OllamaProvider (local)
            │       │
            │       ├── Ollama API (/api/chat)
            │       └── OpenAI-compatible (/v1/chat/completions)
            │
            └── (future) OpenAIProvider (fallback)
```

## BaseLLMProvider

**Файл:** `src/engine/llm/providers/base.py`

Абстрактный базовый класс для LLM провайдеров.

```python
@dataclass
class LLMMessage:
    role: str       # "system", "user", "assistant"
    content: str

@dataclass
class LLMResponse:
    content: str            # Сгенерированный текст
    model: str              # Использованная модель
    tokens_used: int        # Количество токенов
    finish_reason: str      # "stop", "error", "timeout"

class BaseLLMProvider(ABC):
    @abstractmethod
    async def generate(
        messages: List[LLMMessage],
        model?: str,
        temperature: float = 0.7,
        max_tokens: int = 2048
    ) -> LLMResponse

    @abstractmethod
    async def health_check() -> bool

    @abstractmethod
    async def list_models() -> List[str]
```

---

## OllamaProvider

**Файл:** `src/engine/llm/providers/ollama.py`

Провайдер для локальных моделей через Ollama или vLLM.

```python
class OllamaProvider(BaseLLMProvider):
    def __init__(
        base_url: str = "http://localhost:11434",
        default_model: str = "qwen2.5:7b",
        timeout: float = 120.0
    )
```

**Поддерживаемые API:**
1. **Ollama Native** — `/api/chat`
2. **OpenAI-compatible** — `/v1/chat/completions` (для vLLM)

**Конфигурация** (через .env):
```env
LLM_BASE_URL=http://localhost:11434
DEFAULT_MODEL=qwen2.5:7b
```

---

## Использование в ChatService

```python
# При обработке @@ mention
async def _generate_ai_response(chat_id, mentioned_user_id, original_message, org_id):
    # 1. Получить пользователя и его роль
    user = await user_storage.get_by_id(mentioned_user_id)
    role = await role_storage.get_by_id(user.role_id)

    # 2. Подготовить сообщения для LLM
    messages = [
        LLMMessage(role="system", content=role.system_prompt),
        LLMMessage(role="user", content=original_message.content)
    ]

    # 3. Сгенерировать ответ
    llm = OllamaProvider()
    response = await llm.generate(
        messages=messages,
        model=role.model_name
    )

    # 4. Сохранить AI-ответ
    ai_message = Message(
        chat_id=chat_id,
        sender_type=SenderType.AI_ROLE,
        sender_id=mentioned_user_id,
        content=response.content,
        ai_validated=False
    )
    return await message_storage.create(ai_message)
```

---

## Модели

### Локальные (Ollama/vLLM)
- `qwen2.5:7b` — основная модель
- `qwen2.5:72b` — для сложных задач
- `mistral:7b` — альтернатива
- `llama3:8b` — Meta LLaMA

### Облачные (fallback)
- `gpt-4o-mini` — OpenAI (быстрый)
- `gpt-4o` — OpenAI (мощный)

---

## Конфигурация ролей

Каждая роль может использовать свою модель:

```python
Role(
    name="Юрист",
    code="lawyer",
    system_prompt="""Ты корпоративный юрист.
    Помогай с:
    - Анализом договоров
    - Правовыми вопросами
    - Соблюдением законодательства
    Отвечай чётко, ссылайся на законы.""",
    model_name="qwen2.5:72b"  # Для юриста нужна мощная модель
)

Role(
    name="Техподдержка",
    code="support",
    system_prompt="Ты сотрудник техподдержки...",
    model_name="qwen2.5:7b"  # Простые запросы, быстрая модель
)
```

---

## Примеры system prompt

### Юрист
```
Ты корпоративный юрист компании. Твои задачи:
- Анализ и проверка договоров
- Консультации по правовым вопросам
- Помощь с соблюдением законодательства

При ответе:
- Будь точен и конкретен
- Ссылайся на конкретные статьи законов
- Предупреждай о рисках
- Если не уверен — скажи об этом
```

### Бухгалтер
```
Ты корпоративный бухгалтер. Твои задачи:
- Консультации по бухгалтерскому учёту
- Помощь с налоговыми вопросами
- Разъяснение финансовой отчётности

При ответе:
- Ссылайся на стандарты бухучёта
- Указывай применимые налоговые ставки
- Предупреждай о сроках сдачи отчётности
```

### HR
```
Ты HR-специалист компании. Твои задачи:
- Консультации по кадровым вопросам
- Помощь с трудовым законодательством
- Разъяснение корпоративных политик

При ответе:
- Будь вежлив и поддерживающ
- Ссылайся на ТК РФ при необходимости
- Учитывай корпоративную культуру
```

---

## Будущее развитие

### RAG (Retrieval-Augmented Generation)
```
Role.rag_collection → Vector DB (ChromaDB/Milvus)
                          │
                          └── Documents (PDF, DOCX, Excel)
```

**Flow:**
1. Пользователь спрашивает
2. Поиск релевантных документов в RAG
3. Добавление контекста в prompt
4. Генерация ответа LLM
