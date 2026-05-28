#!/usr/bin/env python3
"""Admin CLI for seeding per-customer DCR client credentials.

Seeds pre-created OAuth client credentials into the dcr_clients table
so that different customers get different credentials when DCR requests
come in. The DCR flow checks get_by_order_id() and returns seeded
records automatically without any runtime code changes.

Prerequisites:
    export DATABASE_URL="postgresql+asyncpg://user:pass@host/db"
    export DCR_ENCRYPTION_KEY="<fernet-key>"

Usage:
    # Seed from a JSON file
    python scripts/seed_dcr_clients.py seed --file clients.json

    # Seed a single entry
    python scripts/seed_dcr_clients.py seed \\
        --client-id my-client-id \\
        --client-secret my-secret \\
        --order-id order-123 \\
        --account-id account-456

    # List existing entries
    python scripts/seed_dcr_clients.py list

    # Delete an entry
    python scripts/seed_dcr_clients.py delete --order-id order-123
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import only the ORM model and base -- does NOT trigger get_settings()
from lightspeed_agent.db.base import Base
from lightspeed_agent.db.models import DCRClientModel

DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ClientEntry:
    """A single DCR client entry to seed."""

    client_id: str
    client_secret: str  # plaintext -- encrypted before storage
    order_id: str
    account_id: str
    redirect_uris: list[str] = field(default_factory=list)
    grant_types: list[str] = field(default_factory=lambda: list(DEFAULT_GRANT_TYPES))


# ---------------------------------------------------------------------------
# Encryption (matches DCRService._encrypt_secret at service.py:77-89)
# ---------------------------------------------------------------------------


def get_fernet() -> Fernet:
    """Create a Fernet cipher from DCR_ENCRYPTION_KEY env var."""
    key = os.environ.get("DCR_ENCRYPTION_KEY", "")
    if not key:
        print(
            "ERROR: DCR_ENCRYPTION_KEY is required.\n"
            "Generate one with:\n"
            "  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return Fernet(key.encode())
    except (ValueError, Exception) as e:
        print(f"ERROR: Invalid DCR_ENCRYPTION_KEY: {e}", file=sys.stderr)
        sys.exit(1)


def encrypt_secret(fernet: Fernet, plaintext: str) -> str:
    """Encrypt a client secret for database storage."""
    return fernet.encrypt(plaintext.encode()).decode()


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def load_entries_from_file(path: str) -> list[ClientEntry]:
    """Load client entries from a JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list):
        print("ERROR: JSON file must contain an array of entries", file=sys.stderr)
        sys.exit(1)

    entries = []
    required_fields = {"client_id", "client_secret", "order_id", "account_id"}
    for i, item in enumerate(data):
        missing = required_fields - set(item.keys())
        if missing:
            print(
                f"ERROR: Entry {i} is missing required fields: {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            sys.exit(1)
        entries.append(
            ClientEntry(
                client_id=item["client_id"],
                client_secret=item["client_secret"],
                order_id=item["order_id"],
                account_id=item["account_id"],
                redirect_uris=item.get("redirect_uris", []),
                grant_types=item.get("grant_types", list(DEFAULT_GRANT_TYPES)),
            )
        )
    return entries


def build_entry_from_args(args: argparse.Namespace) -> ClientEntry:
    """Build a single ClientEntry from CLI arguments."""
    return ClientEntry(
        client_id=args.client_id,
        client_secret=args.client_secret,
        order_id=args.order_id,
        account_id=args.account_id,
        redirect_uris=args.redirect_uris or [],
        grant_types=args.grant_types or list(DEFAULT_GRANT_TYPES),
    )


def validate_entries(entries: list[ClientEntry]) -> None:
    """Validate that entries have no duplicate client_ids or order_ids."""
    client_ids = [e.client_id for e in entries]
    order_ids = [e.order_id for e in entries]

    dup_clients = [cid for cid in client_ids if client_ids.count(cid) > 1]
    if dup_clients:
        print(
            f"ERROR: Duplicate client_id(s) in batch: {', '.join(set(dup_clients))}",
            file=sys.stderr,
        )
        sys.exit(1)

    dup_orders = [oid for oid in order_ids if order_ids.count(oid) > 1]
    if dup_orders:
        print(
            f"ERROR: Duplicate order_id(s) in batch: {', '.join(set(dup_orders))}",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_database_url() -> str:
    """Get DATABASE_URL from environment."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print(
            "ERROR: DATABASE_URL is required.\n"
            "Example: export DATABASE_URL='postgresql+asyncpg://user:pass@localhost:5432/dbname'",
            file=sys.stderr,
        )
        sys.exit(1)
    return url


async def create_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """Create an async engine and session factory, ensuring tables exist."""
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


async def seed_entries(
    entries: list[ClientEntry],
    fernet: Fernet,
    dry_run: bool = False,
    skip_existing: bool = False,
) -> None:
    """Insert client entries into the dcr_clients table."""
    database_url = get_database_url()
    factory = await create_session_factory(database_url)

    seeded = 0
    skipped = 0
    errors = 0

    for entry in entries:
        async with factory() as session:
            try:
                # Check if order_id already exists
                result = await session.execute(
                    select(DCRClientModel).where(
                        DCRClientModel.order_id == entry.order_id
                    )
                )
                existing = result.scalar_one_or_none()

                if existing:
                    if skip_existing:
                        print(
                            f"  SKIP: order_id={entry.order_id} already exists "
                            f"(client_id={existing.client_id})"
                        )
                        skipped += 1
                        continue
                    else:
                        print(
                            f"  ERROR: order_id={entry.order_id} already exists "
                            f"(client_id={existing.client_id}). "
                            "Use --skip-existing to skip.",
                            file=sys.stderr,
                        )
                        errors += 1
                        continue

                # Check if client_id already exists
                result = await session.execute(
                    select(DCRClientModel).where(
                        DCRClientModel.client_id == entry.client_id
                    )
                )
                if result.scalar_one_or_none():
                    print(
                        f"  ERROR: client_id={entry.client_id} already exists",
                        file=sys.stderr,
                    )
                    errors += 1
                    continue

                if dry_run:
                    print(
                        f"  DRY RUN: would seed client_id={entry.client_id}, "
                        f"order_id={entry.order_id}, account_id={entry.account_id}"
                    )
                    seeded += 1
                    continue

                model = DCRClientModel(
                    client_id=entry.client_id,
                    client_secret_encrypted=encrypt_secret(fernet, entry.client_secret),
                    order_id=entry.order_id,
                    account_id=entry.account_id,
                    redirect_uris=entry.redirect_uris or None,
                    grant_types=entry.grant_types,
                    registration_access_token_encrypted=None,
                    keycloak_client_uuid=None,
                    metadata_={"seeded_by": "seed_dcr_clients.py"},
                )
                session.add(model)
                await session.commit()
                print(
                    f"  OK: seeded client_id={entry.client_id}, "
                    f"order_id={entry.order_id}"
                )
                seeded += 1

            except IntegrityError as e:
                await session.rollback()
                print(
                    f"  ERROR: integrity error for client_id={entry.client_id}: {e}",
                    file=sys.stderr,
                )
                errors += 1

    prefix = "DRY RUN " if dry_run else ""
    print(f"\n{prefix}Summary: {seeded} seeded, {skipped} skipped, {errors} errors")
    if errors > 0:
        sys.exit(1)


async def list_entries(output_format: str = "table", show_metadata: bool = False) -> None:
    """List all DCR client entries in the database."""
    database_url = get_database_url()
    factory = await create_session_factory(database_url)

    async with factory() as session:
        result = await session.execute(
            select(DCRClientModel).order_by(DCRClientModel.created_at)
        )
        models = result.scalars().all()

    if not models:
        print("No DCR client entries found.")
        return

    if output_format == "json":
        items = []
        for m in models:
            item = {
                "client_id": m.client_id,
                "order_id": m.order_id,
                "account_id": m.account_id,
                "redirect_uris": m.redirect_uris,
                "grant_types": m.grant_types,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            if show_metadata:
                item["metadata"] = m.metadata_
            items.append(item)
        print(json.dumps(items, indent=2))
    else:
        # Table format
        header = f"{'CLIENT_ID':<40} {'ORDER_ID':<25} {'ACCOUNT_ID':<20} {'CREATED_AT':<25}"
        if show_metadata:
            header += f" {'METADATA'}"
        print(header)
        print("-" * len(header))
        for m in models:
            created = m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "N/A"
            line = f"{m.client_id:<40} {m.order_id:<25} {m.account_id:<20} {created:<25}"
            if show_metadata:
                line += f" {json.dumps(m.metadata_)}"
            print(line)
        print(f"\nTotal: {len(models)} entries")


async def delete_entry(
    order_id: str | None = None,
    client_id: str | None = None,
    confirm: bool = False,
) -> None:
    """Delete a DCR client entry."""
    database_url = get_database_url()
    factory = await create_session_factory(database_url)

    async with factory() as session:
        # Find the entry first
        if order_id:
            result = await session.execute(
                select(DCRClientModel).where(DCRClientModel.order_id == order_id)
            )
        else:
            result = await session.execute(
                select(DCRClientModel).where(DCRClientModel.client_id == client_id)
            )
        model = result.scalar_one_or_none()

        if not model:
            identifier = f"order_id={order_id}" if order_id else f"client_id={client_id}"
            print(f"No entry found for {identifier}")
            sys.exit(1)

        print(
            f"Found: client_id={model.client_id}, order_id={model.order_id}, "
            f"account_id={model.account_id}"
        )

        if not confirm:
            answer = input("Delete this entry? [y/N] ").strip().lower()
            if answer != "y":
                print("Cancelled.")
                return

        if order_id:
            await session.execute(
                sa_delete(DCRClientModel).where(DCRClientModel.order_id == order_id)
            )
        else:
            await session.execute(
                sa_delete(DCRClientModel).where(DCRClientModel.client_id == client_id)
            )
        await session.commit()
        print("Deleted.")


# ---------------------------------------------------------------------------
# CLI setup
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Admin tool for managing per-customer DCR client credentials.",
        epilog=(
            "Environment variables:\n"
            "  DATABASE_URL          Database connection string (required)\n"
            "  DCR_ENCRYPTION_KEY    Fernet encryption key for client secrets (required for seed)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- seed ---
    seed_parser = subparsers.add_parser(
        "seed", help="Seed DCR client credentials into the database"
    )
    seed_input = seed_parser.add_mutually_exclusive_group(required=True)
    seed_input.add_argument(
        "--file", help="JSON file with array of client entries"
    )
    seed_input.add_argument(
        "--client-id", help="OAuth client ID (for single entry)"
    )
    seed_parser.add_argument(
        "--client-secret", help="OAuth client secret (for single entry)"
    )
    seed_parser.add_argument(
        "--order-id", help="Marketplace order ID (for single entry)"
    )
    seed_parser.add_argument(
        "--account-id", help="Marketplace account ID (for single entry)"
    )
    seed_parser.add_argument(
        "--redirect-uris", nargs="+", help="OAuth redirect URIs (optional)"
    )
    seed_parser.add_argument(
        "--grant-types", nargs="+", help="OAuth grant types (default: authorization_code refresh_token)"
    )
    seed_parser.add_argument(
        "--dry-run", action="store_true", help="Validate without inserting"
    )
    seed_parser.add_argument(
        "--skip-existing", action="store_true", help="Skip entries whose order_id already exists"
    )

    # --- list ---
    list_parser = subparsers.add_parser(
        "list", help="List existing DCR client entries"
    )
    list_parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format (default: table)"
    )
    list_parser.add_argument(
        "--show-metadata", action="store_true", help="Include metadata in output"
    )

    # --- delete ---
    delete_parser = subparsers.add_parser(
        "delete", help="Delete a DCR client entry"
    )
    delete_target = delete_parser.add_mutually_exclusive_group(required=True)
    delete_target.add_argument("--order-id", help="Delete by order ID")
    delete_target.add_argument("--client-id", help="Delete by client ID")
    delete_parser.add_argument(
        "--confirm", action="store_true", help="Skip confirmation prompt"
    )

    return parser


def main() -> None:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "seed":
        if args.file:
            entries = load_entries_from_file(args.file)
        else:
            # Validate that all single-entry args are provided
            if not all([args.client_id, args.client_secret, args.order_id, args.account_id]):
                parser.error(
                    "--client-id, --client-secret, --order-id, and --account-id "
                    "are all required for single entry mode"
                )
            entries = [build_entry_from_args(args)]

        validate_entries(entries)
        fernet = get_fernet()
        print(f"Seeding {len(entries)} entries...")
        asyncio.run(
            seed_entries(entries, fernet, dry_run=args.dry_run, skip_existing=args.skip_existing)
        )

    elif args.command == "list":
        asyncio.run(list_entries(output_format=args.format, show_metadata=args.show_metadata))

    elif args.command == "delete":
        asyncio.run(
            delete_entry(
                order_id=args.order_id,
                client_id=args.client_id,
                confirm=args.confirm,
            )
        )


if __name__ == "__main__":
    main()
