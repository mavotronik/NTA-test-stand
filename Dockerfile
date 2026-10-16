# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_FILE=/config/config.yaml

RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.example.yaml ./config.example.yaml

# Commit and image build time for the sidebar. .git stays in the build context only.
RUN --mount=type=bind,from=context,source=.,target=/src,ro \
    sh -c 'git -c safe.directory=* -C /src rev-parse HEAD > /app/.panel_commit 2>/dev/null || printf unknown > /app/.panel_commit; date -u +%Y-%m-%dT%H:%M:%SZ > /app/.panel_build_time'

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "from app.config import load_settings; import urllib.request; s=load_settings(); urllib.request.urlopen('http://127.0.0.1:'+str(s.port)+'/api/health').read()"

CMD ["python", "-m", "app"]
