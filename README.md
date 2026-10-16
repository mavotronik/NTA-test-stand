# NTA Test Stand Manager

Панель для нескольких GrapesNTA UI-инстансов на dev/test-стенде: Git-ветки и commit, Docker-сборки из репозитория [GrapesNTA](https://github.com/jzhekatroy/GrapesNTA), профили ClickHouse, мониторинг хоста (CPU/RAM/Disk/LA).

Конфигурация — **`config.yaml`** (секция `manager` + политики стенда).

## Быстрый старт (локально)

```bash
cd /opt/NTA-test-stand
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# git.remote_url — путь к checkout GrapesNTA; manager.bootstrap_admin_password, storage_root
python -m app
```

Порт и bind — из `manager.port` и `manager.host` в config.yaml (пример: 8090).

Откройте UI на порту из `manager.port` — логин `admin`, пароль из `manager.bootstrap_admin_password`.

`CONFIG_FILE=/path/to/config.yaml` переопределяет путь к конфигу.

## Docker

```bash
cp config.example.yaml config.yaml
# manager.host: 0.0.0.0, manager.data_dir: /data, manager.host_proc: /host/proc
# GIT_REPO_HOST_PATH — mount GrapesNTA на хосте
docker compose up -d --build
```

Нужны Docker socket и `/proc:/host/proc:ro` для метрик хоста.

## systemd (без контейнера менеджера)

Сам менеджер — обычный процесс из `.venv`. Docker на хосте по-прежнему нужен для сборки и запуска UI-инстансов. Для метрик без проброса `/proc` задайте `manager.host_proc: /proc` (если `/host/proc` нет, код и так читает `/proc`).

```bash
cd /opt/NTA-test-stand
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # если ещё нет
sudo cp deploy/nta-test-stand.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nta-test-stand.service
```

Логи: `journalctl -u nta-test-stand -f`.

## Тесты

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

Подробнее: [docs/TEST_STAND_CONTROL_PANEL.md](docs/TEST_STAND_CONTROL_PANEL.md).
