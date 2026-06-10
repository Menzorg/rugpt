# GitLab (внутренний git-сервер ИСКРЫ)

Самостоятельный GitLab CE для команды: хранение кода, CI/CD. Полностью изолирован от
остальной инфраструктуры ИСКРЫ (свой PostgreSQL/Redis/nginx внутри), наружу в интернет
не выставлен — веб-морда доступна только через ssh-туннель.

> Установлен 2026-05-31. Редакция: GitLab CE (Omnibus). Раннер: gitlab-runner, shell executor.

---

## 1. Топология

```
Интернет
   │  публичный IP 217.113.118.218
┌──▼─────────────────────────────────────────┐
│  Omada ER605 (роутер, NAT/проброс портов)   │
│  LAN 192.168.1.0/24                         │
└──┬───────────────────────┬──────────────────┘
   │ проброс               │ проброс
   │ 28351 → :22           │ 24952 → :22
┌──▼──────────────────┐  ┌─▼───────────────────┐
│ LXC  rag-gitlab     │  │ VM  rag-worker       │
│ 192.168.1.118       │  │ (gitlab-runner)      │
│ GitLab CE (:8080)   │◄─┤ shell executor       │
│ sshd :22            │  │ ходит в GitLab по    │
│                     │  │ LAN 192.168.1.118:8080│
└─────────────────────┘  └──────────────────────┘
```

- **rag-gitlab** — LXC-контейнер Proxmox, тут сам GitLab. Внутренний IP `192.168.1.118`.
- **rag-worker** — отдельная VM Proxmox, тут CI-раннер.
- Публичный IP `217.113.118.218` принадлежит роутеру **Omada ER605**, который пробрасывает
  порты на гостей. Маршрутизации по поддомену роутер НЕ делает (это L4, не L7) — только по порту.

### DNS

| Запись | Указывает на | Назначение |
|---|---|---|
| `git.iskralink.ru` | `217.113.118.218` (Omada WAN) | git-over-ssh (clone/push) и человекочитаемый хост |

Веб-морда **не** опубликована, отдельной DNS-записи под HTTP/HTTPS нет.

### Проброс портов на Omada

| Внешний порт | Внутренний адрес | Что это |
|---|---|---|
| 28351 | 192.168.1.118:22 | ssh в LXC rag-gitlab (админ + git) |
| 24952 | rag-worker:22 | ssh в VM rag-worker |
| 37256 | rag-docker:22 | ssh в Docker-VM |

Порты 80/443 наружу **не проброшены** — публичного веб-доступа нет by design.

### SSH-алиасы (в `~/.ssh/config` на машине разработчика)

```
Host rag-gitlab
    HostName 217.113.118.218
    User gitlabadmin
    Port 28351
    IdentityFile ~/.ssh/gitlab_petr

Host rag-worker
    HostName 217.113.118.218
    User workeradmin
    Port 24952
    IdentityFile ~/.ssh/gitlabworkers_petr
```

---

## 2. Два канала доступа

Важно не путать — это разные пути, и нужны они для разного:

| Канал | Для чего | Нужен туннель? |
|---|---|---|
| **Веб-морда** (`http://localhost:8080` через туннель) | UI: код, MR, настройки, CI | Да |
| **git-over-ssh** (`git@git.iskralink.ru:28351`) | clone / push / pull | Нет — идёт напрямую |

### Веб-морда — только через ssh-туннель

Наружу 80/443 закрыты. Чтобы открыть UI, разработчик поднимает локальный проброс порта
до GitLab и ходит на свой `localhost`. Шифрует сам ssh — публичный TLS не нужен.

```
ssh -i ~/.ssh/iskra_tunnel -N -L 8080:127.0.0.1:8080 -p 28351 tunnel@git.iskralink.ru
```

Туннель «висит» (так и надо), в браузере — `http://localhost:8080`.

Все форвардят именно на **8080**, потому что `external_url = http://localhost:8080`.
У каждого это свой локальный `localhost`, конфликта нет.

#### Изолированный туннель-юзер `tunnel`

Доступ к туннелю даётся через выделенного системного пользователя `tunnel` на LXC,
у которого ключи ограничены так, что им можно **только** пробросить порт 8080 и больше
ничего (ни шелла, ни других портов). Ограничения — опциями в `authorized_keys`:

```
restrict,port-forwarding,permitopen="127.0.0.1:8080",command="/bin/false" ssh-ed25519 AAAA...ключ... метка
```

- `restrict` — выключает всё (pty, agent/X11/port-forwarding, ~/.ssh/rc);
- `port-forwarding` — возвращает только проброс портов;
- `permitopen="127.0.0.1:8080"` — разрешает `-L` только на 8080;
- `command="/bin/false"` — закрывает шелл (один `restrict` его НЕ блокирует — он режет
  только pty, поэтому без `command` оставался бы безпарольный шелл).

Файл: `/home/tunnel/.ssh/authorized_keys`. **Одна строка на разработчика** (см. онбординг).

### git-over-ssh — напрямую, без туннеля

```
git clone ssh://git@git.iskralink.ru:28351/<группа>/<проект>.git
```

Работает так: `git.iskralink.ru` → `217.113.118.218` → Omada `28351` → sshd LXC `:22`,
логин пользователем `git` → системный sshd через gitlab-shell отдаёт репозиторий.
Пользователь `git` создан Omnibus'ом, ключи он ведёт сам в `~git/.ssh/authorized_keys`
по ключам, которые разработчик загрузил **в свой профиль GitLab**.

Проверка канала:

```
ssh -T -i ~/.ssh/<ключ> -p 28351 git@git.iskralink.ru
# → Welcome to GitLab, @username!
```

---

## 3. Конфигурация GitLab (`/etc/gitlab/gitlab.rb`)

Значимые настройки и зачем они:

```ruby
external_url 'http://localhost:8080'         # доступ через туннель, без публичного веба
puma['port'] = 8081                          # ОБЯЗАТЕЛЬНО: дефолтный puma-порт 8080
                                             #   конфликтует с nginx на 8080 → crash-loop
nginx['listen_port'] = 8080
nginx['listen_https'] = false                # TLS даёт ssh-туннель, не nginx
letsencrypt['enable'] = false                # публичного домена/HTTPS нет
gitlab_rails['gitlab_ssh_host'] = 'git.iskralink.ru'   # для правильного clone-URL по ssh
gitlab_rails['gitlab_shell_ssh_port'] = 28351
puma['worker_processes'] = 2                 # тюнинг под 8 ГБ RAM
prometheus_monitoring['enable'] = false      # экономия памяти
```

После любой правки: `sudo gitlab-ctl reconfigure`.

> Дорого менять задним числом только `external_url` — он зашит в clone-URL, письма,
> редиректы. Остальное (registry, prometheus, тюнинг) — свободно в любой момент.

Встроенные компоненты (всё под `/opt/gitlab`, управляются `gitlab-ctl`, не системными
сервисами): PostgreSQL, Redis, nginx, Puma, Sidekiq, Gitaly, Workhorse. Системные
postgres/redis/nginx на этом боксе не используются.

---

## 4. CI-раннер (на rag-worker)

- Пакет `gitlab-runner`, работает как **системный сервис** (systemd), конфиг
  `/etc/gitlab-runner/config.toml`. Вручную `gitlab-runner run` запускать НЕ нужно.
- Зарегистрирован как **MainWorker**, executor **shell**, URL `http://192.168.1.118:8080`
  (внутренний IP GitLab по LAN, не `localhost`!).
- Тип в GitLab: **instance runner** (shared, для всех проектов), «Run untagged jobs» включено.

### Ключевая настройка `clone_url`

В `[[runners]]` в `config.toml`:

```toml
clone_url = "http://192.168.1.118:8080"
```

**Обязательно.** Без неё GitLab отдаёт раннеру clone-URL на основе `external_url`
(`http://localhost:8080`), а на воркере `localhost` — это он сам, и клонирование падает.
`clone_url` переписывает хост на LAN-адрес GitLab.

### Регистрация раннера (новый flow, GitLab 17+)

Старый `--registration-token` убран. Порядок:

1. В GitLab: **Admin → CI/CD → Runners → New instance runner** → получить токен `glrt-...`.
2. На rag-worker: `sudo gitlab-runner register` → URL `http://192.168.1.118:8080`,
   token `glrt-...`, executor `shell`.
3. Дописать `clone_url` в `config.toml`, затем `sudo gitlab-runner restart`.

Проверка: `sudo gitlab-runner list`, `sudo gitlab-runner verify`,
`sudo systemctl status gitlab-runner`.

---

## 5. Онбординг разработчика

GitLab-аккаунт — **персональный у каждого** (атрибуция, права, отзыв). Общие аккаунты нет.

1. **Админ**: создаёт пользователя — **Admin → Users → New user**, задаёт роль.
   (Пока не настроен SMTP — пароль передаётся вручную; после SMTP заработают приглашения.)
2. **Разработчик**: генерит ssh-ключ, грузит **публичную** часть в свой профиль GitLab
   (*Edit profile → SSH Keys*) — это для clone/push.
3. **Доступ к веб-морде**: разработчик присылает свой публичный ключ для туннеля; **админ**
   дописывает ОДНУ строку в `/home/tunnel/.ssh/authorized_keys` на rag-gitlab:
   ```
   restrict,port-forwarding,permitopen="127.0.0.1:8080",command="/bin/false" ssh-ed25519 AAAA...ключ... имя
   ```
   После правки права: `chown -R tunnel:tunnel /home/tunnel/.ssh`, `chmod 700 .ssh`,
   `chmod 600 authorized_keys`.
4. **Разработчик**: поднимает туннель и ходит на `http://localhost:8080`:
   ```
   ssh -i ~/.ssh/<туннель-ключ> -N -L 8080:127.0.0.1:8080 -p 28351 tunnel@git.iskralink.ru
   ```

**Отзыв доступа**: выключить аккаунт в GitLab + удалить строку ключа из
`/home/tunnel/.ssh/authorized_keys`. Персональные ключи → отзыв точечный, без ротации у всех.

Один и тот же ключ можно использовать и для git (профиль GitLab), и для туннеля
(`authorized_keys` юзера `tunnel`) — на усмотрение; раздельные чуть аккуратнее.

---

## 6. Эксплуатация

На rag-gitlab (`ssh rag-gitlab`):

```bash
sudo gitlab-ctl status              # состояние всех сервисов (все должны быть run:)
sudo gitlab-ctl reconfigure         # применить изменения gitlab.rb
sudo gitlab-ctl restart             # перезапустить весь GitLab
sudo gitlab-ctl restart puma        # перезапустить один сервис
sudo gitlab-ctl tail puma           # живой лог сервиса
sudo cat /etc/gitlab/initial_root_password   # стартовый пароль root (живёт 24 ч)
sudo gitlab-rake "gitlab:password:reset[root]"  # сброс пароля
```

Логи: `/var/log/gitlab/<сервис>/current` (puma, nginx, gitaly, sidekiq, ...).

На rag-worker (`ssh rag-worker`):

```bash
sudo gitlab-runner list
sudo gitlab-runner verify
sudo systemctl status gitlab-runner
```

---

## 7. Траблшутинг (по уже встреченному)

- **`Permission denied (publickey)` при ssh** — чаще всего права на приватный ключ.
  `chmod 600 ~/.ssh/<ключ>` (ssh молча игнорирует слишком открытый ключ).
- **502 «Waiting for GitLab to boot» долго не проходит** — `sudo gitlab-ctl status`.
  Если у `puma` аптайм постоянно сбрасывается — он в crash-loop. Смотреть
  `/var/log/gitlab/puma/current`.
  - `Errno::EADDRINUSE ... port 8080` — конфликт портов puma↔nginx. Решение: `puma['port'] = 8081`.
  - Не OOM ли — `free -h` (на 8 ГБ важно; если память впритык и swap пуст — урезать воркеры).
- **CI-job падает на клонировании** — проверить `clone_url` в `config.toml` раннера
  (должен быть LAN-адрес GitLab, не `localhost`).
- **Раннер не берёт задачи** — включён ли «Run untagged jobs» (если у job нет тегов),
  `sudo gitlab-runner verify`, `systemctl status gitlab-runner`.
- **Туннель: `connection refused` на localhost:8080** — туннель не запущен/прибит.
  `ssh -N -L ...` должен висеть в отдельном окне, пока работаешь.

---

## 8. Ещё не сделано (TODO)

- **Бэкапы** — не настроены. Нужны `gitlab-backup create` + cron, и **отдельно** бэкап
  секретов `/etc/gitlab/gitlab-secrets.json` и `/etc/gitlab/gitlab.rb` (без них бэкап
  не восстановить). Приоритет №1.
- **SMTP/почта** — не настроена. Без неё не уходят приглашения, сброс пароля, уведомления.
- **Container Registry** — выключен. Для CI-only по локалке поднимается через
  `insecure-registries` на rag-worker + Docker/kaniko там же (внешний TLS не нужен,
  пока образы не тянут на машины разработчиков).
- **request_concurrency** раннера — сейчас 1; можно поднять до 2-4 для более быстрого
  подхвата задач.
- **Tailscale/VPN вместо per-dev туннелей** — на вырост, если команда разрастётся и
  раздача туннель-ключей станет рутиной.

---

## См. также

- `docs/networking.md` — сетевая топология инфраструктуры
- `docs/architecture.md` — общая архитектура
