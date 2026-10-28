"""Auth bootstrap CLI.

Manage users and API keys backed by Postgres (requires DATABASE_URL).

Examples:
    python -m scripts.auth_cli bootstrap-admin --username admin --password 'change-me'
    python -m scripts.auth_cli add-user --username alice --password 'p@ssw0rd' --role user
    python -m scripts.auth_cli add-key --username alice --name "alice laptop" --role user
    python -m scripts.auth_cli list-users
    python -m scripts.auth_cli list-keys
    python -m scripts.auth_cli revoke-key --key-id <id>
"""

from __future__ import annotations

import argparse
import sys

from settings import get_settings
from store.auth_store import (
    AuthStore,
    DuplicateUserError,
    InvalidCredentialsError,
    UserNotFoundError,
)


def _store() -> AuthStore:
    url = get_settings().database_url
    if not url:
        print(
            "error: DATABASE_URL is not set; auth requires Postgres.\n"
            "Set DATABASE_URL (e.g. postgresql+psycopg://browser:browser@127.0.0.1:5432/browser_agent).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return AuthStore(url)


def cmd_bootstrap_admin(args: argparse.Namespace) -> None:
    store = _store()
    try:
        row = store.create_user(
            username=args.username, password=args.password, role="admin"
        )
    except DuplicateUserError:
        existing = store.get_user_by_username(args.username)
        if existing is not None and existing.role == "admin":
            print(f"admin user '{args.username}' already exists (role=admin)")
            return
        print(
            f"error: username '{args.username}' taken but not an admin; "
            "use add-user with an explicit role instead.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"created admin user: {row.username} (user_id={row.user_id})")


def cmd_add_user(args: argparse.Namespace) -> None:
    store = _store()
    try:
        row = store.create_user(
            username=args.username,
            password=args.password,
            role=args.role,
        )
    except DuplicateUserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"created user: {row.username} role={row.role} (user_id={row.user_id})")


def cmd_add_key(args: argparse.Namespace) -> None:
    store = _store()
    owner = store.get_user_by_username(args.username)
    if owner is None:
        raise UserNotFoundError(f"user not found: {args.username}")
    data, plain = store.create_api_key(
        owner_id=owner.user_id,
        name=args.name,
        role=args.role,
        expires_at=args.expires_at,
    )
    print(f"created api key: {data['name']} (key_id={data['key_id']}, prefix={data['key_prefix']})")
    print(f"plaintext (shown once): {plain}")


def cmd_list_users(_: argparse.Namespace) -> None:
    for row in _store().list_users():
        print(f"{row.user_id}\t{row.username}\t{row.role}\t{row.status}\t{row.created_at}")


def cmd_list_keys(_: argparse.Namespace) -> None:
    for data in _store().list_api_keys():
        revoked = "revoked" if data["revoked_at"] else "active"
        print(
            f"{data['key_id']}\t{data['name']}\tprefix={data['key_prefix']}\t"
            f"owner={data['owner_id']}\trole={data['role'] or '-'}\t{revoked}"
        )


def cmd_revoke_key(args: argparse.Namespace) -> None:
    if _store().revoke_api_key(args.key_id):
        print(f"revoked api key: {args.key_id}")
    else:
        print(f"error: api key not found: {args.key_id}", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap-admin", help="create the first admin user")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.set_defaults(func=cmd_bootstrap_admin)

    p = sub.add_parser("add-user", help="create a user")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--role", default="user", choices=["admin", "user", "read_only"])
    p.set_defaults(func=cmd_add_user)

    p = sub.add_parser("add-key", help="create an API key for a user")
    p.add_argument("--username", required=True)
    p.add_argument("--name", default="default")
    p.add_argument("--role", choices=["admin", "user", "read_only"], default=None)
    p.add_argument("--expires-at", default=None, help="ISO/UTC string or empty")
    p.set_defaults(func=cmd_add_key)

    p = sub.add_parser("list-users", help="list users")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("list-keys", help="list API keys")
    p.set_defaults(func=cmd_list_keys)

    p = sub.add_parser("revoke-key", help="revoke an API key")
    p.add_argument("--key-id", required=True)
    p.set_defaults(func=cmd_revoke_key)

    args = parser.parse_args()
    try:
        args.func(args)
    except (UserNotFoundError, InvalidCredentialsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
