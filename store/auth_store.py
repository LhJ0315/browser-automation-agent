"""Auth store: users + API keys, backed by Postgres (same DATABASE_URL).

The auth backend is available when ``DATABASE_URL`` is configured. Without it
the API falls back to static ``API_KEYS`` (admin-level, unowned).
"""

from __future__ import annotations

import hashlib
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import String, Text, create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from api.security import generate_api_key, hash_password, verify_password
from browser_agent.archive import utc_now
from settings import get_settings
from store.postgres_task_store import Base, run_migrations


class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AuthStoreError(Exception):
    pass


class DuplicateUserError(AuthStoreError):
    pass


class UserNotFoundError(AuthStoreError):
    pass


class InvalidCredentialsError(AuthStoreError):
    pass


class AuthStore:
    """Postgres-backed users + api_keys repository."""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self.database_url = database_url
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        run_migrations(database_url)

    # ---- users ----

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str = "user",
        user_id: str | None = None,
    ) -> UserRow:
        from api.security import ROLES

        role = (role or "user").strip().lower()
        if role not in ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {sorted(ROLES)}")
        now = utc_now()
        with self._Session() as session:
            exists = session.scalar(
                select(UserRow).where(UserRow.username == username)
            )
            if exists is not None:
                raise DuplicateUserError(f"username already taken: {username}")
            row = UserRow(
                user_id=user_id or str(uuid.uuid4()),
                username=username,
                password_hash=hash_password(password),
                role=role,
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return row

    def get_user(self, user_id: str) -> UserRow | None:
        with self._Session() as session:
            return session.get(UserRow, user_id)

    def get_user_by_username(self, username: str) -> UserRow | None:
        with self._Session() as session:
            return session.scalar(
                select(UserRow).where(UserRow.username == username)
            )

    def list_users(self) -> list[UserRow]:
        with self._Session() as session:
            return list(
                session.scalars(
                    select(UserRow).order_by(UserRow.created_at.desc())
                ).all()
            )

    def update_user(self, user_id: str, **fields: Any) -> UserRow:
        allowed = {"role", "status", "password"}
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                raise AttributeError(key)
            if key == "password":
                updates["password_hash"] = hash_password(str(value))
            else:
                updates[key] = value
        with self._Session() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                raise UserNotFoundError(f"user not found: {user_id}")
            if "password_hash" in updates:
                row.password_hash = updates.pop("password_hash")
            for k, v in updates.items():
                setattr(row, k, v)
            row.updated_at = utc_now()
            session.commit()
            return row

    def authenticate(self, username: str, password: str) -> UserRow:
        row = self.get_user_by_username(username)
        if row is None or not verify_password(password, row.password_hash):
            raise InvalidCredentialsError("invalid username or password")
        if row.status != "active":
            raise InvalidCredentialsError("user is disabled")
        return row

    # ---- api keys ----

    def create_api_key(
        self,
        *,
        owner_id: str,
        name: str,
        role: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Create an API key; returns (row_dict, plaintext). Plaintext once."""
        plain, digest = generate_api_key()
        now = utc_now()
        row = ApiKeyRow(
            key_id=str(uuid.uuid4()),
            name=name.strip() or "unnamed",
            key_prefix=plain[:8],
            key_hash=digest,
            owner_id=owner_id,
            role=role,
            expires_at=expires_at,
            revoked_at=None,
            last_used_at=None,
            created_at=now,
        )
        with self._Session() as session:
            session.add(row)
            session.commit()
            return self._key_row_to_dict(row), plain

    def _key_row_to_dict(self, row: ApiKeyRow) -> dict[str, Any]:
        return {
            "key_id": row.key_id,
            "name": row.name,
            "key_prefix": row.key_prefix,
            "owner_id": row.owner_id,
            "role": row.role,
            "expires_at": row.expires_at,
            "revoked_at": row.revoked_at,
            "last_used_at": row.last_used_at,
            "created_at": row.created_at,
        }

    def list_api_keys(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        with self._Session() as session:
            stmt = select(ApiKeyRow).order_by(ApiKeyRow.created_at.desc())
            if owner_id:
                stmt = stmt.where(ApiKeyRow.owner_id == owner_id)
            return [self._key_row_to_dict(r) for r in session.scalars(stmt).all()]

    def revoke_api_key(self, key_id: str) -> bool:
        with self._Session() as session:
            row = session.get(ApiKeyRow, key_id)
            if row is None:
                return False
            row.revoked_at = utc_now()
            session.commit()
            return True

    def authenticate_key(self, plain: str) -> dict[str, Any] | None:
        """Validate a raw API key; returns effective identity dict or None."""
        digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
        with self._Session() as session:
            row = session.scalar(
                select(ApiKeyRow).where(ApiKeyRow.key_hash == digest)
            )
            if row is None or row.revoked_at:
                return None
            if row.expires_at and row.expires_at < utc_now():
                return None
            owner = session.get(UserRow, row.owner_id)
            if owner is None or owner.status != "active":
                return None
            session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.key_id == row.key_id)
                .values(last_used_at=utc_now())
            )
            session.commit()
            return {
                "key_id": row.key_id,
                "owner_id": row.owner_id,
                "username": owner.username,
                "user_role": owner.role,
                "role": row.role or owner.role,
            }


def build_auth_store(database_url: str | None) -> AuthStore | None:
    """None when no Postgres is configured -> static API_KEYS mode."""
    if not (database_url or "").strip():
        return None
    return AuthStore(database_url)


@lru_cache(maxsize=1)
def get_auth_store() -> AuthStore | None:
    """Process-wide auth store singleton; None disables DB auth."""
    return build_auth_store(get_settings().database_url)


def reset_auth_store_cache() -> None:
    """Test helper: clear the cached singleton."""
    get_auth_store.cache_clear()
