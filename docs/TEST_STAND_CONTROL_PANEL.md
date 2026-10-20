# Test Stand Control Panel

Панель управления dev/test-стендом GrapesNTA (отдельный репозиторий [NTA-test-stand](https://github.com/mavotronik/NTA-test-stand)).

Сборка UI по-прежнему идёт из checkout **GrapesNTA** (`git.remote_url` → `deploy/ui/app` на выбранном commit).

## Возможности MVP

- Несколько UI-инстансов (`grapes-test-<slug>`), host network, свой `PORT` и каталог данных.
- Профили ClickHouse через UI (шифрование пароля, проверка `SELECT version()`).
- Очередь сборок и развёртываний, логи задач, откат на предыдущий образ.
- RBAC: admin / operator / viewer, сессии, CSRF на POST.
- Аудит действий пользователей.
- **Мониторинг хоста:** CPU, RAM, load average, disk — обновление через HTMX каждые N секунд (`host_metrics.refresh_sec` в `config.yaml`).

## Безопасность

- Контейнер менеджера с Docker socket = высокий привилегированный доступ. Используйте только во внутренней сети/VPN.
- Не монтируйте production write-профили без необходимости; writable-профили помечаются явно.
- `manager.secret_key` и опционально `manager.fernet_key` храните только в `config.yaml` (файл не коммитится).
- Резервное копирование: каталог `manager.data_dir` (SQLite) и ключ шифрования.

## Конфиг (`config.yaml`)

Скопируйте `config.example.yaml`. Основные поля:

- `manager` — порт, `data_dir`, секреты, bootstrap admin, `host_proc` для метрик.
- `git.remote_url` — путь к checkout GrapesNTA на хосте или URL для clone.
- `ports.min/max` — диапазон портов инстансов.
- `storage_root` — данные инстансов на хосте.
- `host_metrics.disk_paths` — точки для `disk_usage`.

Переопределение пути: переменная окружения `CONFIG_FILE` (в Docker Compose уже `/config/config.yaml`).

Сборка UI: при наличии `docker compose` на хосте — как `./deploy/deploy.sh ui` в GrapesNTA; иначе tar-контекст через socket. Перед стартом инстанса каталог данных на хосте создаётся с `chown 1001:1001`, bind-mount в `/app/server/data`.

`manager.data_dir` и `storage_root` (относительные пути) разрешаются от каталога `config.yaml`. Для Docker Compose менеджера задайте `manager.data_dir: /data` и `storage_root: /data/root` (volume `./data:/data`).

## Типовой сценарий

1. Создать профиль БД → «Проверить».
2. Создать инстанс (slug, port, ветка или **версия (тег)** из списка; опционально `git.allowed_branch_patterns` / `git.allowed_tag_patterns` в config.yaml — пустой список или отсутствие ключа снимает фильтр).
3. «Собрать образ» → дождаться `ready` в «Сборки».
4. «Развернуть» → healthcheck `/api/health` на порту инстанса.
5. При ошибке — «Откатить образ» (если сохранён предыдущий tag).

## Ограничения

- Схема ClickHouse не мигрируется автоматически при deploy.
- Rollback не откатывает изменения в БД.
- UI берётся из vendored `deploy/ui/app` в репозитории GrapesNTA на выбранном commit.
