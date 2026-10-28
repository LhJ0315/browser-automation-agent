"""Auth dependencies: resolve a Bearer credential to a Principal.

Resolution order:
1. JWT access token (``POST /api/v1/auth/login``).
2. Database-backed API key (when ``DATABASE_URL`` is configured).
3. Static env ``API_KEYS`` set (admin-level, unowned, legacy P1 behavior).

No credentials + no configured auth (neither DB nor static keys) keeps the
legacy local-open behavior: everything is allowed and treated as static admin.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.security import Principal, decode_access_token, looks_like_jwt
from settings import get_settings
from store.auth_store import get_auth_store

_bearer = HTTPBearer(auto_error=False)


def parse_api_keys(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _static_keys() -> frozenset[str]:
    return parse_api_keys(get_settings().api_keys)


def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Legacy static-key check (kept for tests / compatibility)."""
    keys = _static_keys()
    if not keys:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if credentials.credentials not in keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _unauthorized(detail: str = "missing or invalid credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    auth_store = get_auth_store()
    static_keys = _static_keys()
    token = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials.strip()

    # 1) JWT access token (works in every mode when presented).
    if token and looks_like_jwt(token):
        try:
            return Principal.from_payload(decode_access_token(token))
        except HTTPException:
            pass  # invalid/expired -> try key paths below

    # 2) Database-backed API key.
    if token and auth_store is not None:
        ident = auth_store.authenticate_key(token)
        if ident is not None:
            return Principal(
                principal_id=ident["owner_id"],
                username=ident["username"],
                role=ident["role"],
                auth_type="api_key",
                is_admin=ident["role"] == "admin",
            )

    # 3) Static env keys (admin-level).
    if token and token in static_keys:
        return Principal.static_admin()

    # 4) Legacy open-local mode: no DB backend and no static keys -> allow all.
    if auth_store is None and not static_keys:
        return Principal.static_admin()

    raise _unauthorized()
