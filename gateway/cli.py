"""Admin CLI (until the /admin API lands in M2).

python -m gateway.cli create-key --name demo --models groq-gpt-oss-20b,gemini-flash
python -m gateway.cli list-keys
"""

import argparse
import asyncio
from decimal import Decimal

from gateway.core.config import get_settings
from gateway.db.repositories import SqlKeyStore
from gateway.db.session import make_engine, make_sessionmaker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-key", help="create an API key (printed once)")
    create.add_argument("--name", required=True)
    create.add_argument("--rpm", type=int)
    create.add_argument("--tpm", type=int)
    create.add_argument("--spend-cap", type=Decimal, help="monthly cap in USD")
    create.add_argument("--models", help="comma-separated aliases; omit for all")
    sub.add_parser("list-keys")
    return parser


async def _main(args: argparse.Namespace) -> None:
    engine = make_engine(get_settings().database_url)
    store = SqlKeyStore(make_sessionmaker(engine))
    try:
        if args.command == "create-key":
            models = [m.strip() for m in args.models.split(",")] if args.models else None
            raw, record = await store.create(
                args.name,
                rpm_limit=args.rpm,
                tpm_limit=args.tpm,
                monthly_spend_cap_usd=args.spend_cap,
                allowed_models=models,
            )
            print(f"id:  {record.id}")
            print(f"key: {raw}")
            print("Store this key now; it cannot be shown again.")
        else:
            for prefix, record in await store.list_all():
                state = "active" if record.active else "revoked"
                allowed = ",".join(sorted(record.allowed_models or ["*"]))
                print(f"{record.id}  {prefix}...  {state:7}  {record.name}  models={allowed}")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    main()
