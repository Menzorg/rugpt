# Совместная разработка RuGPT

## Обзор

На dev-сервере работают двое разработчиков под отдельными Linux-пользователями, но с единым Claude-аккаунтом (шаринг токена авторизации).

## Пользователи

| Пользователь | UID | Home | Роль |
|---|---|---|---|
| `root` | 0 | `/root` | Основной разработчик |
| `wolflord` | 1000 | `/home/wolflord` | Второй разработчик |

## SSH-доступ

- Ключ: `ed25519`
- Публичный ключ: `/home/wolflord/.ssh/authorized_keys`
- Подключение: `ssh wolflord@<host>`
- Через VS Code: расширение Remote-SSH, добавить хост в `~/.ssh/config`

Приватный ключ генерируется однократно и передаётся разработчику безопасным каналом. На сервере приватный ключ не хранится.

## Claude Code

- Установлен через native installer в `/home/wolflord/.local/bin/claude`
- Версия синхронно обновляется с root при запуске `claude`
- Оба пользователя работают под одним Anthropic-аккаунтом
- Истории чатов, проекты и `TODO` — раздельные (каждый в своём `~/.claude/projects/`)

## Синхронизация Claude credentials

OAuth-токен в `.credentials.json` периодически обновляется Claude Code через refresh token. При использовании одного аккаунта с двух сторон возникает гонка: тот, кто обновляет первым, инвалидирует refresh_token у второго → 401.

Решение — двусторонняя inotify-синхронизация в реальном времени.

### Компоненты

- Скрипт: `/usr/local/bin/sync-claude-creds.sh`
- Unit: `/etc/systemd/system/claude-creds-sync.service`
- Следит за: `/root/.claude/` и `/home/wolflord/.claude/`
- События: `close_write`, `moved_to`, `create` на `.credentials.json`
- Защита от петель: `cmp -s` — копирование только при реальном различии

### Логика

```
изменение у root → копия в /home/wolflord/.claude/ (chown wolflord, 600)
изменение у wolflord → копия в /root/.claude/ (chown root, 600)
```

### Управление

```bash
systemctl status claude-creds-sync          # статус демона
systemctl restart claude-creds-sync         # перезапуск
journalctl -u claude-creds-sync -f          # логи сервиса
journalctl -t claude-creds-sync -f          # события синхронизации
```

Автозапуск при ребуте включён.

## Файлы проектов

Проекты скопированы в home-директорию wolflord **без** тяжёлых/чувствительных директорий:

### RuGPT Engine

- Путь: `/home/wolflord/rugpt/`
- Исключено при копировании: `venv`, `.git`, `logs`, `__pycache__`, `.env*`, `*.pyc`
- Размер: ~2 МБ (против 302 МБ с venv)

### WebClient RuGPT

- Путь: `/home/wolflord/webclient_rugpt/`
- Исключено при копировании: `node_modules`, `.git`, `logs`, `dist`, `build`, `.env*`
- Размер: ~3 МБ (против 920 МБ с node_modules)

## Настройка окружения разработчика

После копирования проектов каждый настраивает своё окружение независимо.

### Python (Engine)

```bash
cd ~/rugpt
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

venv содержит захардкоженные пути, поэтому должен создаваться локально, а не копироваться.

### Node.js (WebClient)

Node 24 LTS через nvm (установлен в `/home/wolflord/.nvm/`, автоподключается через `.bashrc`).

```bash
cd ~/webclient_rugpt
npm install       # workspaces monorepo — ставит backend/frontend/common/shared разом
```

### Секреты (.env)

`.env` файлы не копируются автоматически. Передаются защищённым каналом либо генерируются заново из примеров.

## Обновление кода у wolflord (rsync)

Актуальная версия кода живёт у root (`/root/rugpt`, `/root/webclient_rugpt`). Чтобы докатить её в `/home/wolflord/...`, используется `rsync` с теми же исключениями, что и при первичном копировании.

### Engine

```bash
rsync -a --delete \
  --exclude='venv' \
  --exclude='.git' \
  --exclude='logs' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='*.pyc' \
  --exclude='.env*' \
  /root/rugpt/ /home/wolflord/rugpt/
chown -R wolflord:wolflord /home/wolflord/rugpt
```

### WebClient

```bash
rsync -a --delete \
  --exclude='node_modules' \
  --exclude='.git' \
  --exclude='logs' \
  --exclude='dist' \
  --exclude='build' \
  --exclude='.next' \
  --exclude='.env*' \
  /root/webclient_rugpt/ /home/wolflord/webclient_rugpt/
chown -R wolflord:wolflord /home/wolflord/webclient_rugpt
```

### Опции

- `-a` — archive mode (сохраняет права, таймстампы, symlinks)
- `--delete` — удаляет у wolflord файлы, которых уже нет у root (полная синхронизация). Убрать, если нужно "докинуть" без удаления локальных файлов wolflord.
- Слэш в конце `/root/rugpt/` обязателен — означает «содержимое директории», а не саму директорию.

### Dry-run

Перед реальным запуском посмотреть что изменится:

```bash
rsync -an --delete --exclude='venv' ... /root/rugpt/ /home/wolflord/rugpt/
```

Флаг `-n` (dry-run) показывает план без выполнения.

### Важные нюансы

- Python `venv` не синхронизируется — пути захардкожены. После крупных изменений `requirements.txt` wolflord переустанавливает пакеты: `pip install -r requirements.txt` в своём venv.
- `node_modules` не синхронизируется — после изменений `package.json` wolflord запускает `npm install`.
- `.env` не синхронизируются — секреты у каждого свои.
- Запущенные процессы (uvicorn, vite, nest) во время rsync не перезапускаются автоматически — нужно рестартовать руками.

## Ограничения

- Оба пользователя логинят одну и ту же БД `rugpt` с общими данными (`psql -U postgres -h localhost`).
- Запуск dev-сервера одновременно на одних и тех же портах (`8100` для Engine) невозможен — либо договариваться, либо поднимать второй инстанс на другом порту и с отдельной БД.
- Rate limits Claude-аккаунта общие на обоих — при одновременной интенсивной работе быстрее упираются в лимиты.
