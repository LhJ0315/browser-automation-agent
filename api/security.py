"""Security primitives: password hashing, JWT codec, principal model.

No external crypto dependency beyond PyJWT + stdlib hashlib.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, status

from settings import get_settings

_PBKDF2_ROUNDS = 600_000


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds
    )
    return f"pbkdf2$sha256${rounds}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, hash_name, rounds_s, salt, expected = encoded.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2" or hash_name != "sha256":
        return False
    try:
        rounds = int(rounds_s)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds
    )
    return hmac.compare_digest(digest.hex(), expected)


# ---- JWT ----

ACCESS_TOKEN_TYPE = "access"


def _jwt_secret() -> str:
    secret = get_settings().jwt_secret
    return secret or "browser-agent-dev-secret-change-me"


def create_access_token(
    *, user_id: str, username: str, role: str, expires_minutes: int | None = None
) -> tuple[str, int]:
    """Return (token, ttl_seconds)."""
    cfg = get_settings()
    ttl_min = expires_minutes or cfg.jwt_expires_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_min),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token, ttl_min * 60


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.split(".")[0].startswith("eyJ")


# ---- Principal ----

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_READ_ONLY = "read_only"
ROLES = frozenset({ROLE_ADMIN, ROLE_USER, ROLE_READ_ONLY})


@dataclass(frozen=True)
class Principal:
    """Who is calling the API."""

    principal_id: str  # user_id, or "static" for env-level API keys
    username: str | None
    role: str
    auth_type: str  # jwt | api_key | static_key
    is_admin: bool = False

    @classmethod
    def static_admin(cls) -> "Principal":
        return cls(
            principal_id="static",
            username=None,
            role=ROLE_ADMIN,
            auth_type="static_key",
            is_admin=True,
        )

    @classmethod
    def from_payload(cls, payload: dict) -> "Principal":
        role = payload.get("role") or ROLE_USER
        return cls(
            principal_id=str(payload.get("sub") or ""),
            username=payload.get("username"),
            role=role,
            auth_type="jwt",
            is_admin=role == ROLE_ADMIN,
        )

    @classmethod
    def from_api_key_row(cls, row) -> "Principal":
        role = (row.role or row.user_role or ROLE_USER) if hasattr(row, "user_role") else (row.role or ROLE_USER)
        return cls(
            principal_id=row.owner_id or "static",
            username=getattr(row, "owner_username", None),
            role=role,
            auth_type="api_key",
            is_admin=role == ROLE_ADMIN,
        )

    @property
    def can_write(self) -> bool:
        return self.role != ROLE_READ_ONLY

    @property
    def sees_all(self) -> bool:
        """Static env key or admin: no ownership scoping."""
        return self.auth_type == "static_key" or self.is_admin


def require_can_write(principal: Principal) -> None:
    if not principal.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="read-only role cannot modify resources",
        )


def require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )


def generate_api_key() -> tuple[str, str]:
    """Return (plaintext, sha256_hex). Plaintext shown exactly once."""
    plain = "bak_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return plain, digest


def key_prefix(plain: str) -> str:
    return plain[: 8 + 4]  # "bak_xxxx" visible fragment
