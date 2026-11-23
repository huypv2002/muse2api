"""Client đăng nhập gói luồng trên Cloudflare Worker + D1."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_AUTH_URL = "https://muse-auth.kh431248.workers.dev"


class AuthError(Exception):
    pass


def _request(base: str, path: str, method: str = "GET", body: dict | None = None, token: str = "") -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 MuseDesktop",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise AuthError(message) from exc
    except urllib.error.URLError as exc:
        raise AuthError("Không kết nối được máy chủ đăng nhập.") from exc
    return json.loads(raw) if raw else {}


def login(base: str, username: str, password: str) -> dict:
    return _request(base, "/login", "POST", {"username": username, "password": password})


def me(base: str, token: str) -> dict:
    return _request(base, "/me", "GET", token=token)


def logout(base: str, token: str) -> None:
    _request(base, "/logout", "POST", token=token)
