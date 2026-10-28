"""Auth routes: login (JWT), /me, user & API-key management (admin)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from api.auth import get_principal
from api.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeySummary,
    LoginRequest,
    MeResponse,
    TokenResponse,
    UserCreateRequest,
    UserOut,
)
from api.security import (
    Principal,
    create_access_token,
    require_admin,
)
from store.auth_store import (
    DuplicateUserError,
    InvalidCredentialsError,
    UserNotFoundError,
    get_auth_store,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _store():
    store = get_auth_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="auth requires DATABASE_URL (Postgres) to be configured",
        )
    return store


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    store = _store()
    try:
        user = store.authenticate(body.username, body.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token, ttl = create_access_token(
        user_id=user.user_id, username=user.username, role=user.role
    )
    return TokenResponse(
        access_token=token,
        expires_in=ttl,
        user=UserOut(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
        ),
    )


@router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(get_principal)) -> MeResponse:
    return MeResponse(
        principal_id=principal.principal_id,
        username=principal.username,
        role=principal.role,
        auth_type=principal.auth_type,
        is_admin=principal.is_admin,
    )


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreateRequest, principal: Principal = Depends(get_principal)
) -> UserOut:
    require_admin(principal)
    store = _store()
    try:
        row = store.create_user(
            username=body.username, password=body.password, role=body.role
        )
    except DuplicateUserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UserOut(
        user_id=row.user_id,
        username=row.username,
        role=row.role,
        status=row.status,
        created_at=row.created_at,
    )


@router.get("/users", response_model=list[UserOut])
def list_users(principal: Principal = Depends(get_principal)) -> list[UserOut]:
    require_admin(principal)
    rows = _store().list_users()
    return [
        UserOut(
            user_id=r.user_id,
            username=r.username,
            role=r.role,
            status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=201)
def create_api_key(
    body: ApiKeyCreateRequest, principal: Principal = Depends(get_principal)
) -> ApiKeyCreatedResponse:
    require_admin(principal)
    store = _store()
    owner = store.get_user_by_username(body.owner_username)
    if owner is None:
        raise HTTPException(status_code=404, detail="owner user not found")
    data, plain = store.create_api_key(
        owner_id=owner.user_id,
        name=body.name,
        role=body.role,
        expires_at=body.expires_at or None,
    )
    return ApiKeyCreatedResponse(
        key_id=data["key_id"],
        name=data["name"],
        key_prefix=data["key_prefix"],
        owner_id=data["owner_id"],
        owner_username=owner.username,
        role=data["role"],
        created_at=data["created_at"],
        api_key=plain,
    )


@router.get("/api-keys", response_model=list[ApiKeySummary])
def list_api_keys(
    principal: Principal = Depends(get_principal),
) -> list[ApiKeySummary]:
    require_admin(principal)
    return [
        ApiKeySummary(**data)
        for data in _store().list_api_keys()
    ]


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: str, principal: Principal = Depends(get_principal)
) -> None:
    require_admin(principal)
    if not _store().revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="api key not found")
