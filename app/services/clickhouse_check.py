from __future__ import annotations

import httpx

from app.config import Policy, host_allowed


async def check_clickhouse_connection(
    policy: Policy,
    *,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    use_tls: bool,
) -> tuple[bool, str]:
    if not host_allowed(policy, host):
        return False, f"host {host} not in allowlist"
    scheme = "https" if use_tls else "http"
    url = f"{scheme}://{host}:{port}/"
    params = {"query": "SELECT version() AS version", "database": database}
    try:
        async with httpx.AsyncClient(timeout=policy.ch_connect_timeout_sec) as client:
            resp = await client.get(url, params=params, auth=(username, password))
            if resp.status_code == 401:
                return False, "authentication failed"
            if resp.status_code >= 400:
                return False, f"HTTP {resp.status_code}"
            if "version" not in resp.text.lower() and resp.text.strip() == "":
                return False, "empty response"
            return True, resp.text.strip()[:120]
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.HTTPError as err:
        return False, str(err)
