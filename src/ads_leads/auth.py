"""Вход по логину и паролю: страница входа + подписанная cookie-сессия.

Пользователи задаются в .env:
  ADS_LEADS_AUTH_PASSWORD=...            — общий логин ADS_LEADS_AUTH_USER (по умолчанию adbeam)
  ADS_LEADS_AUTH_USERS=stas:pw1,nonna:pw2 — личные логины (данные у всех общие)
Если ни один пароль не задан — вход выключен (локальный запуск).
"""

import base64
import hashlib
import hmac
import secrets
import time

from ads_leads.config import Settings

COOKIE_NAME = "ads_leads_session"
SESSION_TTL = 30 * 24 * 3600  # 30 дней


def get_users(settings: Settings) -> dict[str, str]:
    users: dict[str, str] = {}
    if settings.auth_password:
        users[settings.auth_user] = settings.auth_password
    for pair in settings.auth_users.split(","):
        login, sep, password = pair.strip().partition(":")
        if sep and login.strip() and password:
            users[login.strip()] = password
    return users


def check_credentials(settings: Settings, login: str, password: str) -> bool:
    expected = get_users(settings).get(login.strip())
    return expected is not None and secrets.compare_digest(password.encode(), expected.encode())


def _secret(settings: Settings) -> bytes:
    # Секрет зависит от паролей: смена пароля разлогинивает всех.
    base = settings.secret_key or "|".join(f"{k}:{v}" for k, v in sorted(get_users(settings).items()))
    return hashlib.sha256(("ads-leads:" + base).encode()).digest()


def make_session(settings: Settings, login: str) -> str:
    payload = f"{login}|{int(time.time()) + SESSION_TTL}".encode()
    sig = hmac.new(_secret(settings), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode() + "." + sig


def read_session(settings: Settings, token: str | None) -> str | None:
    """Логин из cookie, если подпись верна, срок не истёк и пользователь ещё существует."""
    if not token or "." not in token:
        return None
    raw, _, sig = token.rpartition(".")
    try:
        payload = base64.urlsafe_b64decode(raw.encode())
    except ValueError:
        return None
    expected = hmac.new(_secret(settings), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    login, _, expires = payload.decode(errors="replace").rpartition("|")
    if not expires.isdigit() or int(expires) < time.time():
        return None
    return login if login in get_users(settings) else None
