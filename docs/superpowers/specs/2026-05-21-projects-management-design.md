# Управление проектами (создание / редактирование / удаление + видимость «мои»)

**Дата:** 2026-05-21
**Статус:** на ревью
**Затрагивает:** Engine (`/root/rugpt`), WebClient frontend (`/root/webclient_rugpt`). NestJS-прокси — без изменений.

## 1. Контекст и проблема

Проекты (item 11) — способ группировки задач; у проекта есть свой чат. На фронте сейчас НЕТ полноценного управления проектами:
- **Создание** — только инлайн в модалке создания задачи (под-форма «новый проект»), отдельной точки нет.
- **Список** — `useProjects` (GET `/api/projects`) используется как фильтр/пикер задач и для чатов проектов в сайдбаре; отдельной страницы/вкладки нет.
- **Редактирование / удаление** — на фронте отсутствуют (`useProjects` отдаёт только `create`).
- **Чат проекта** — есть (`chat/project/[id]`).

Текущая модель движка:
- `GET /projects` → `list_by_org` — возвращает **все** проекты орги (нет фильтра «мои»).
- `POST/PATCH/DELETE /projects` → `_check_can_create`: **только head/admin**. → обычный юзер не может создать проект (403); инлайн-создание в форме задачи у обычных юзеров не работает.

Цель: вкладка «Проекты» в странице задач с полным CRUD и видимостью «доступные мне» проекты.

## 2. Принятые решения

0. **Проект хранит свой отдел.** Новое поле `projects.department_id` (NULL-able), проставляется при создании = `creator.department_id`, далее **заморожено** (не плывёт при смене/удалении создателя). Это «отдел-владелец» проекта; на нём строятся видимость head'а и право правки.
1. **Права:**
   - **создание** — любой авторизованный юзер.
   - **редактирование/удаление** — создатель проекта; **admin** (любой проект орги); **head** — проекты **его отдела** (`project.department_id == head.department_id`), плюс свои созданные. (head НЕ правит проекты чужих отделов.)
2. **Видимость списка — три уровня:**
   - **admin** (руководитель организации) → все проекты орги.
   - **head** (руководитель отдела) → проекты его отдела (`project.department_id == department_id head'а`), ПЛЮС личное участие (см. ниже). Если у head'а `department_id` пуст — деградирует до правила обычного юзера.
   - **обычный юзер** → проект виден, если `created_by_user_id == me` ИЛИ в проекте есть активная задача, где я **создатель, исполнитель или участник**.
   - Личное участие («created_by==me ИЛИ участие через задачу») действует для всех, включая head — поэтому head, втянутый в задачу проекта чужого отдела, тоже видит такой проект.
3. **Размещение UI:** вкладка «Проекты» в существующем таб-баре страницы задач (`/tasks`).
4. **Операции:** создание (отдельная точка), редактирование (имя+описание), удаление/архив, просмотр списка.
5. `GET /projects/{id}` и чат проекта остаются **org-level** (не скоупим), чтобы ссылки `!!project` и прямые переходы не ломались. Скоуп применяется только к **списку**.

## 3. Дизайн по слоям

### 3.0 Engine — миграция: `projects.department_id`

- Новая миграция (следующий свободный номер): `ALTER TABLE projects ADD COLUMN department_id UUID REFERENCES departments(id) ON DELETE SET NULL;` + индекс `(department_id)`.
- Backfill существующих: `UPDATE projects p SET department_id = (SELECT u.department_id FROM users u WHERE u.id = p.created_by_user_id)` (проекты с NULL-создателем/без отдела → остаётся NULL).
- Модель `models/project.py`: поле `department_id: Optional[UUID]`; `to_dict` → ключ `department_id` (str|None); `from_dict` принимает его. `ProjectStorage.create`/`update`/`_row_to_project` — провести колонку.

### 3.1 Engine — права (`services/project_service.py`)

- `create(name, user, description)`: убрать вызов `self._check_can_create(user)`. Проставлять `department_id=user.department_id` (отдел создателя на момент создания). Остальное без изменений (`created_by_user_id=user.id`, `org_id=user.org_id`).
- Новый helper `_check_can_modify(project, user)` (**синхронный**, без чтения создателя):
  - `user.is_admin` → разрешить (любой проект).
  - `project.created_by_user_id == user.id` → разрешить (свой).
  - `user.is_head and user.department_id is not None and project.department_id == user.department_id` → разрешить.
  - иначе → `PermissionError("Only the creator, the head of the project's department, or an admin can modify this project")`.
  - `user_storage` НЕ нужен — сравнение по хранимому `project.department_id`.
- `update(...)`: порядок — `get_by_id`; если None → `ValueError(not found)`; `_check_same_org`; затем `_check_can_modify`; потом применять изменения. (Сейчас `_check_can_create` стоит ПЕРЕД загрузкой — переставить.)
- `delete(...)`: аналогично — `get_by_id` → not found → `_check_same_org` → `_check_can_modify` → `deactivate` + `archive_project_chat`.
- `_check_can_create` удалить (больше не используется).

### 3.2 Engine — видимость списка

- Новый storage-метод `ProjectStorage.list_visible_for_user(user_id, org_id, include_archived=False, department_id=None) -> List[Project]`. Опциональный `department_id` включает ветку «проекты моего отдела» (для head'а) — по хранимому `p.department_id`, без join к `users`:
  ```sql
  SELECT * FROM projects p
  WHERE p.org_id = $2
    AND (p.is_active OR $3)            -- $3 = include_archived
    AND (
      p.created_by_user_id = $1
      OR ($4::uuid IS NOT NULL AND p.department_id = $4)   -- $4 = department_id (head)
      OR p.id IN (
        SELECT DISTINCT t.project_id FROM tasks t
        WHERE t.is_active AND t.project_id IS NOT NULL
          AND (
            t.created_by_user_id = $1
            OR t.assignee_user_id = $1
            OR EXISTS (SELECT 1 FROM task_participants tp
                       WHERE tp.task_id = t.id AND tp.user_id = $1)
          )
      )
    )
  ORDER BY p.created_at DESC
  ```
  (Паттерн участия — как в `task_storage.py` involvement-проверках. `$4=NULL` → ветка отдела отключена.)
- Новый service-метод `ProjectService.list_visible(user, include_archived)`:
  - `user.is_admin` → `list_by_org(user.org_id, include_archived)` (вся орга).
  - `user.is_head and user.department_id is not None` → `list_visible_for_user(user.id, user.org_id, include_archived, department_id=user.department_id)`.
  - иначе (обычный юзер / head без отдела) → `list_visible_for_user(user.id, user.org_id, include_archived, department_id=None)`.
- Роут `GET /projects` (`routes/projects.py`): загрузить `user` через `_load_user`; вернуть `list_visible(user, include_archived)`. (Сейчас зовёт `list_by_org` без загрузки user.)

### 3.3 NestJS — без изменений

Прокси-маршруты полные: `get_projects / get_project / create / update_project / delete_project / get_project_chat` (adapter `rugpt.adapter.ts:849-878`), контроллер с `@Get/@Post/@Patch/@Delete`. **Нужно:** в маппинге проекта (NestJS `project.service.ts`) убедиться/добавить поля `createdByUserId` (`created_by_user_id`) и `departmentId` (`department_id`) — оба нужны фронту для гейта кнопок и бейджа «другой отдел». Плюс `common/src/types/project.ts` `Project` — добавить `departmentId?: string | null`.

### 3.4 Frontend

- `hooks/useProjects.ts`: добавить
  - `update(id, data: {name?: string; description?: string})` → signedPatch `/api/projects/{id}`, заменить в локальном списке.
  - `remove(id)` → signed DELETE `/api/projects/{id}`, убрать из локального списка.
  - (Существующие `projects`, `loading`, `refetch`, `create` — без изменений; список теперь per-viewer приходит с движка.)
- `tasks/page.tsx`: добавить вкладку **«Проекты»** в таб-бар.
  - Контент вкладки — список проектов (`useProjects`): карточка `name` + `description` + дата; действия:
    - «Открыть чат» → переход на `/chat/project/{id}` (страница чата проекта сама резолвит проект и чат, как сейчас).
    - «Создать проект» (кнопка над списком) → модалка (имя + описание) → `create`.
    - «Редактировать» (модалка rename+описание → `update`) и «Архивировать» (confirm → `remove`) — показываем по гейту ниже.
  - **Гейт кнопок edit/archive** (зеркало движка). Резолвим себя из `useUsers` (орг-список несёт `departmentId`/`isHead`): `me = users.find(id==currentUser.id)`. Отдел проекта берём прямо из payload — `project.departmentId`.
    `canModify = me.isAdmin || project.createdByUserId === me.id || (me.isHead && me.departmentId && project.departmentId === me.departmentId)`.
    Движок энфорсит независимо (403).
  - **Пометка «другой отдел»** (чисто фронт, дёшево): если `project.departmentId` задан и ≠ `me.departmentId` — показать на карточке бейдж «Другой отдел» + название отдела (`project.departmentId` → `useDepartments`/`deptMap`). Если `project.departmentId` пуст — бейдж не показываем. Так head, втянутый личным участием в проект чужого отдела, видит его помеченным.
  - Декомпозиция: вынести список/карточку/модалки в отдельный компонент (напр. `components/ProjectsTab.tsx` + модалка `ProjectFormModal.tsx`), чтобы не раздувать `tasks/page.tsx`.
- Существующее инлайн-создание проекта в форме задачи — оставить (после ослабления прав в движке оно заработает у всех).

## 4. Тестирование

**Engine (pytest, idiom как в support-тестах — реальная dev-БД для storage, моки для прав):**
- `create` любым юзером (не head/admin) — успех, `created_by_user_id` и `department_id` (= отдел создателя) проставлены.
- `update`/`delete`:
  - создателем — ok; чужим обычным юзером — `PermissionError`/403;
  - admin — ok для любого проекта;
  - head — ok для проекта с `department_id == его отдел`; 403 для проекта чужого отдела;
  - head без `department_id` — ведёт себя как обычный (только свои).
- `list_visible_for_user`:
  - видит созданные собой; видит проект, где он assignee/creator/participant активной задачи; НЕ видит чужой проект без его участия; архивные — только при `include_archived`;
  - с `department_id` (head): дополнительно видит проекты с `p.department_id == его отдел` (даже без личного участия); НЕ видит проекты чужого отдела без участия.
- `list_visible` (роутинг по роли): admin → вся орга; head → ветка с `department_id`; обычный → без неё.

**Frontend:** `tsc --noEmit` + `next build`; ручная проверка вкладки (создать/переименовать/архивировать; гейт кнопок; видимость списка под обычным юзером vs head/admin).

## 5. Вне скоупа

- Скоуп доступа к `GET /projects/{id}` и чату проекта (остаются org-level).
- Перенос/массовые операции с задачами проекта, аналитика по проекту.
- Отдельная страница `/projects` (выбрана вкладка в задачах).

## 6. Риски

- **Миграция + backfill `projects.department_id`** — backfill заполняет из текущего отдела создателя на момент миграции (это и есть «заморозка»). Проекты с NULL-создателем или создателем без отдела → `department_id = NULL` (управляемы только создателем/admin, в dept-ветку видимости не попадают). Согласовано.
- **`ProjectService` `user_storage` НЕ нужен** — отдел берётся из хранимого `project.department_id`, без чтения создателя. (Ранее рассматривали резолв на лету — отклонили.)
- **`createdByUserId` и `departmentId` в ответе проекта** — оба нужны фронту (гейт кнопок + бейдж «другой отдел»). Проверить/добавить в NestJS-маппинг `project.service.ts` и в `common` `Project`-тип. Без них гейт/бейдж сломаются (движок всё равно защитит).
- **`departmentId`/`isHead` в `/api/users`** — фронт-гейт берёт их из орг-списка `useUsers` (для текущего юзера). Тип `User` их несёт и сайдбар уже использует (★ для head, группировка по отделам), значит приходят; формально подтвердить при реализации.
- **Видимость через задачи и архивные проекты** — `list_visible_for_user` учитывает только активные задачи (`t.is_active`); проект, где все задачи архивны, но created_by==me — виден (через ветку created_by). Согласовано.
- **Инлайн-создание в форме задачи** теперь доступно всем — это намеренный побочный эффект ослабления прав, не регресс.
