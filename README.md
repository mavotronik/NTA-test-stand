# Test Stand Manager

Внутренняя панель для нескольких GrapesNTA UI-инстансов: Git-ветки, Docker-сборки, профили ClickHouse, мониторинг хоста (CPU/RAM/Disk/LA) в реальном времени.

Конфигурация — один файл **`config.yaml`** (секция `manager` + политики стенда).

## Быстрый старт (локально)

```bash
cd test-stand/control-panel
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# отредактируйте config.yaml: manager.bootstrap_admin_password, git.remote_url, storage_root
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Откройте http://127.0.0.1:8080 — логин `admin`, пароль из `manager.bootstrap_admin_password`.

Путь к конфигу можно переопределить: `CONFIG_FILE=/path/to/config.yaml`.

## Docker

```bash
cp config.example.yaml config.yaml
# для контейнера: manager.host: 0.0.0.0, manager.data_dir: /data, manager.host_proc: /host/proc
docker compose up -d --build
```

Нужны Docker socket и mount `/proc:/host/proc:ro` для метрик хоста.

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

См. [docs/TEST_STAND_CONTROL_PANEL.md](../../docs/TEST_STAND_CONTROL_PANEL.md).
