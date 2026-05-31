# Дев-стенд ИСКРЫ + CD-пайплайн — план реализации

> Это ops-план (провижен удалённых машин + GitLab CI), не TDD-код. Шаги выполняет
> ПОЛЬЗОВАТЕЛЬ на указанной машине; Claude даёт команды и проверяет вывод.
> Метки исполнителя: `[МАК]`, `[rag-dev]`, `[rag-worker]`, `[GitLab UI]`.
> Спека: `docs/superpowers/specs/2026-05-31-iskra-dev-stand-cd-design.md`.

**Цель:** деплой движка (и затем вебклиента) на дев-стенд `rag-dev` по пушу в ветку `dev`,
через раннер `MainWorker`, вариант B (rsync чекаута → `rag-dev` → рестарт).

**Архитектура:** движок — venv+screen на хосте `rag-dev`; зависимости (PostgreSQL+pgvector,
Redis, Kafka) и вебклиент — в Docker на `rag-dev`. Пайплайн = адаптированный `deploy.sh`.

**Факты окружения:**
- `rag-dev`: LAN `192.168.1.85`; снаружи `217.113.118.218:20935`, юзер `rugptdev`, ключ `~/.ssh/rugptdev_petr`. Docker 29.1.3 + compose v2.40.3, `rugptdev` в группе docker.
- `rag-worker`: раннер `MainWorker` (shell), берёт untagged-задачи. До `rag-dev` ходит по LAN `192.168.1.85:22`.
- GitLab: `git.iskralink.ru:28351`, репо `iskra/engine`, `iskra/webclient`, группа `iskra`.
- `docker-compose.dev.yml` уже в репо движка (postgres+pgvector/redis/kafka; читает `DB_*` из `.env` движка; `KAFKA_ADVERTISED_HOST` по умолч. `192.168.1.85`).
- Движок: venv, `python -m src.engine.run` в screen `rugpt-engine`, health `:8100/api/v1/health`. `local_restart.sh engine` = stop+миграции+start+healthcheck.

---

## Фаза 0 — Предусловия

- [ ] **0.1 `[rag-worker]` Установить rsync на раннере** (для деплоя)

```
sudo apt update && sudo apt install -y rsync
```
Проверка: `rsync --version` → печатает версию.

- [ ] **0.2 `[rag-dev]` Установить rsync + инструменты сборки Python**

```
sudo apt update && sudo apt install -y rsync python3-venv python3-dev build-essential
```
Проверка: `rsync --version` и `python3 -m venv --help` отрабатывают без ошибок.
(build-essential/python3-dev нужны для нативных колёс вроде asyncpg.)

---

## Фаза 1 — Deploy-ключ раннер→rag-dev

- [ ] **1.1 `[МАК]` Сгенерировать keypair без пароля** (CI пароль не введёт)

```
ssh-keygen -t ed25519 -f ~/.ssh/iskra_deploy -C iskra-deploy -N ""
```
Проверка: `ls ~/.ssh/iskra_deploy*` → два файла (приватный + `.pub`).

- [ ] **1.2 `[МАК]` Показать публичную часть**

```
cat ~/.ssh/iskra_deploy.pub
```
Скопировать строку.

- [ ] **1.3 `[rag-dev]` Завести публичный ключ для юзера `rugptdev`**

```
mkdir -p ~/.ssh && nano ~/.ssh/authorized_keys
```
Вставить строку из 1.2 отдельной строкой, сохранить. Затем:
```
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
```

- [ ] **1.4 `[МАК]` Проверить ключ снаружи (через Omada)**

```
ssh -i ~/.ssh/iskra_deploy -p 20935 rugptdev@217.113.118.218 hostname
```
Ожидание: печатает `rugptdev` (имя хоста), без запроса пароля.

- [ ] **1.5 `[GitLab UI]` Положить приватный ключ в переменную CI/CD на уровне группы**

Группа `iskra` → **Settings → CI/CD → Variables → Add variable**:
- Key: `DEV_SSH_KEY`
- Type: **File**
- Value: полное содержимое `~/.ssh/iskra_deploy` (приватный ключ; `[МАК] cat ~/.ssh/iskra_deploy`)
- Flags: **Protected** ✓, Expand ✗ (Masked недоступен для File)

Группа, а не проект — чтобы и `iskra/engine`, и `iskra/webclient` видели один ключ.

---

## Фаза 2 — Ветка dev + пайплайн движка

- [ ] **2.1 `[МАК]` Создать и запушить ветку `dev`** (в клоне `iskra/engine`)

```
git checkout -b dev
git push -u origin dev
```
Проверка: ветка `dev` видна в GitLab → Code → Branches.

- [ ] **2.2 `[GitLab UI]` Защитить ветку `dev`**

`iskra/engine` → **Settings → Repository → Protected branches** → добавить `dev`
(чтобы protected-переменная `DEV_SSH_KEY` отдавалась на ней).

- [ ] **2.3 `[МАК]` Добавить `docker-compose.dev.yml` в репо** (если ещё не в клоне)

Содержимое — точно как в файле репо `docker-compose.dev.yml` (postgres+pgvector/redis/kafka,
читает `DB_NAME/DB_USER/DB_PASSWORD` из `.env`, `KAFKA_ADVERTISED_HOST` дефолт `192.168.1.85`).
Если файла нет в маковском клоне — создать с этим содержимым.

- [ ] **2.4 `[МАК]` Заменить `.gitlab-ci.yml`** деплой-пайплайном

Полное содержимое:
```yaml
# CD: деплой движка на дев-стенд (rag-dev) при пуше в ветку dev.
# Раннер MainWorker (shell) на rag-worker → rsync на rag-dev по LAN → local_restart.sh.
stages:
  - deploy

deploy-dev:
  stage: deploy
  rules:
    - if: '$CI_COMMIT_BRANCH == "dev"'
  variables:
    DEV_HOST: "192.168.1.85"
    DEV_USER: "rugptdev"
    DEV_PATH: "/home/rugptdev/rugpt"
    GIT_DEPTH: "20"
  script:
    - chmod 600 "$DEV_SSH_KEY"
    - SSHOPT="-i $DEV_SSH_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    - rsync -az --exclude venv --exclude .git --exclude '*.log' --exclude .env --exclude __pycache__ --exclude uploads --exclude logs -e "ssh $SSHOPT" ./ "$DEV_USER@$DEV_HOST:$DEV_PATH/"
    - rsync -az --delete -e "ssh $SSHOPT" ./src/engine/migrations/ "$DEV_USER@$DEV_HOST:$DEV_PATH/src/engine/migrations/"
    - ssh $SSHOPT "$DEV_USER@$DEV_HOST" "cd $DEV_PATH && if [ -d venv ]; then venv/bin/pip install -q -r requirements.txt && ./local_restart.sh engine; else echo 'venv отсутствует — выполните разовый bootstrap (.env, deps up, venv), затем повторите деплой'; fi"
```
Заметки:
- `DEV_SSH_KEY` — File-переменная, `$DEV_SSH_KEY` это путь к временному файлу с ключом.
- основной `rsync` БЕЗ `--delete` (иначе снёс бы ручной `.env` и `uploads`); `--delete` только для зеркала миграций — как в `deploy.sh`.
- если `venv` ещё нет (первый прогон) — рестарт пропускается, пайплайн зелёный.

- [ ] **2.5 `[МАК]` Закоммитить и запушить в `dev`**

```
git add docker-compose.dev.yml .gitlab-ci.yml
git commit -m "ci: деплой движка на дев-стенд из ветки dev"
git push
```
Ожидание: в GitLab → Build → Pipelines стартует пайплайн на `dev`.

- [ ] **2.6 `[GitLab UI]` Проверить первый прогон (полубутстрап)**

Job `deploy-dev` должен **пройти зелёным**: rsync залил код в `~/rugpt` на `rag-dev`,
а на шаге рестарта — сообщение `venv отсутствует — выполните разовый bootstrap...`.
Проверка `[rag-dev]`: `ls ~/rugpt` → код движка на месте (`src/`, `requirements.txt`, `docker-compose.dev.yml`, `local_restart.sh`).

---

## Фаза 3 — Разовый bootstrap rag-dev

- [ ] **3.1 `[rag-dev]` Создать `~/rugpt/.env`** (вручную, значения с прода)

```
cd ~/rugpt && nano .env
```
Обязательные ключи (значения — из прод-`.env`, скорректированные под дев):
- `API_HOST=0.0.0.0`  ← чтобы контейнеры вебклиента достучались через host.docker.internal
- `API_PORT=8100`
- `DB_HOST=localhost`, `DB_PORT=5432`, `DB_NAME=rugpt`, `DB_USER=postgres`, `DB_PASSWORD=<задать>` (он же пойдёт в Postgres-контейнер)
- `REDIS_URL=redis://localhost:6379/0`
- `KAFKA_ENABLED=true`, `KAFKA_BOOTSTRAP_SERVERS=192.168.1.85:9092`
- `LLM_BASE_URL=http://192.168.1.80:4000/v1`, `DEFAULT_MODEL=<с прода>`, `EMBEDDING_MODEL=<с прода>`, `RAG_VECTOR_DIM=1024`
- `JWT_*` и прочие секреты — с прода.
(Не выдумывать значения — взять реальные с прод-`.env`.)

- [ ] **3.2 `[rag-dev]` Поднять зависимости в Docker**

```
cd ~/rugpt && docker compose -f docker-compose.dev.yml up -d
```
Проверка:
```
docker compose -f docker-compose.dev.yml ps
```
Ожидание: `iskra-dev-postgres` (healthy через ~15с), `iskra-dev-redis`, `iskra-dev-kafka` — все `running`.

- [ ] **3.3 `[rag-dev]` Проверить, что pgvector и БД доступны**

```
docker exec iskra-dev-postgres psql -U postgres -d rugpt -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname='vector';"
```
Ожидание: печатает версию pgvector (например `0.7.x`). (Миграции потом сделают это сами, это лишь sanity-check.)

- [ ] **3.4 `[rag-dev]` Создать venv и поставить зависимости**

```
cd ~/rugpt && python3 -m venv venv && venv/bin/pip install -U pip && venv/bin/pip install -r requirements.txt
```
Ожидание: установка без ошибок (на это и ставили build-essential/python3-dev в 0.2).

- [ ] **3.5 `[rag-dev]` Первый запуск движка вручную** (миграции + старт)

```
cd ~/rugpt && ./local_restart.sh engine
```
Ожидание: в конце `Engine API запущен и отвечает`, health на `:8100`.
Проверка: `curl -s http://localhost:8100/api/v1/health` → JSON со `status`.
Если упал — `screen -r rugpt-engine` (логи), разобрать.

---

## Фаза 4 — Замкнуть пайплайн движка

- [ ] **4.1 `[GitLab UI]` Перезапустить деплой** (теперь venv есть)

`iskra/engine` → Build → Pipelines → последний на `dev` → **Retry** job `deploy-dev`.
(Или сделать пустой коммит в `dev` и запушить.)

- [ ] **4.2 `[GitLab UI]` Проверить, что рестарт отработал**

В логе job на шаге ssh: `pip install` + вывод `local_restart.sh` (`Engine API запущен и отвечает`).
Job зелёный.

- [ ] **4.3 `[rag-dev]` Подтвердить деплой**

```
curl -s http://localhost:8100/api/v1/health
screen -ls
```
Ожидание: health отвечает, есть screen `rugpt-engine`.

- [ ] **4.4 Тест полного цикла**

`[МАК]` мелкое изменение в `dev` (например правка README) → `git push` → пайплайн →
`[rag-dev]` `curl` health подтверждает, что движок перезапущен. **Деплой движка готов.**

---

## Фаза 5 — Вебклиент (тот же шаблон)

Отличия: путь `~/webclient`, рантайм Docker (2 контейнера), рестарт = `docker compose build && up -d`.

- [ ] **5.1 `[МАК]` Ветка `dev` в `iskra/webclient`** (создать, запушить, защитить как в 2.1–2.2).

- [ ] **5.2 `[rag-dev]` Подготовить env вебклиента** (вручную, по аналогии с переездом на iskralink):
  - `.env` рядом с `docker-compose.yml`: `NEXT_PUBLIC_BACKEND_URL=<адрес дев-движка>`
  - `packages/backend/.docker.env`: `FRONTEND_URL=<адрес дев-фронта>`, `RUGPT_ENGINE_URL=http://192.168.1.85:8100` (движок на хосте)
  - Конкретные адреса зависят от модели доступа к дев-вебклиенту (открытый пункт спеки — решить здесь).

- [ ] **5.3 `[МАК]` `.gitlab-ci.yml` вебклиента** на ветке `dev`:
```yaml
stages:
  - deploy

deploy-dev:
  stage: deploy
  rules:
    - if: '$CI_COMMIT_BRANCH == "dev"'
  variables:
    DEV_HOST: "192.168.1.85"
    DEV_USER: "rugptdev"
    DEV_PATH: "/home/rugptdev/webclient"
  script:
    - chmod 600 "$DEV_SSH_KEY"
    - SSHOPT="-i $DEV_SSH_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    - rsync -az --exclude node_modules --exclude .git --exclude '*.log' --exclude .env --exclude '.docker.env' -e "ssh $SSHOPT" ./ "$DEV_USER@$DEV_HOST:$DEV_PATH/"
    - ssh $SSHOPT "$DEV_USER@$DEV_HOST" "cd $DEV_PATH && docker compose build && docker compose up -d"
```
(rsync исключает `node_modules` и env-файлы — они на `rag-dev` вручную. Фронт пересобирается через `build`, т.к. `NEXT_PUBLIC_*` зашивается на сборке.)

- [ ] **5.4 `[МАК]` Запушить → первый деплой вебклиента**, проверить, что контейнеры поднялись (`[rag-dev] docker compose ps` в `~/webclient`).

---

## Открытые пункты (решить по ходу)

- Значения `.env` движка и вебклиента — берёт владелец с прода, не выдумываем.
- Модель доступа к дев-вебклиенту (ssh-туннель как к GitLab / порт на LAN) — решить в 5.2.
- `request_concurrency` раннера, бэкапы дев-БД — вне этого плана.

## Self-review (покрытие спеки)

- Вариант B (rsync) — Фаза 2/4 ✓; движок venv+screen — Фаза 3 ✓; deps в Docker — Фаза 3.2 ✓;
  pgvector — 3.3 ✓; `.env` файлом вручную — 3.1 ✓; ключ как protected-переменная + ветка protected — 1.5/2.2 ✓;
  замена старого `.gitlab-ci.yml` — 2.4 ✓; вебклиент тем же шаблоном — Фаза 5 ✓.
- Плейсхолдеров нет (значения `.env` сознательно отданы владельцу — это требование «не выдумывать»).
