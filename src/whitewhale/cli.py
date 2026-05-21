"""White Whale command-line interface."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path

import click

from whitewhale import config as config_loader
from whitewhale import db as db_module
from whitewhale.ingest.rtds import RTDSClient, persist


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@click.group()
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a config YAML. Defaults to config/default.yaml.",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """White Whale - Polymarket whale alert system."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config_loader.load(config_path)


@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Create the SQLite schema. Idempotent."""
    db_path = ctx.obj["config"]["db"]["path"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    click.echo(f"schema initialized at {db_path}")


@main.command("tap")
@click.option("--limit", type=int, default=10, help="Stop after N messages.")
@click.pass_context
def tap(ctx: click.Context, limit: int) -> None:
    """Print raw RTDS messages to stdout. Use this to verify wire format."""
    cfg = ctx.obj["config"]["ingest"]
    client = RTDSClient(
        url=cfg["rtds_url"],
        max_backoff_seconds=cfg["reconnect_max_backoff_seconds"],
    )

    async def _run() -> None:
        seen = 0
        async for msg in client.raw_messages():
            click.echo(json.dumps(msg, indent=2))
            seen += 1
            if seen >= limit:
                client.stop()
                break

    asyncio.run(_run())


@main.command("ingest")
@click.pass_context
def ingest(ctx: click.Context) -> None:
    """Stream RTDS trades and persist them to SQLite indefinitely."""
    cfg = ctx.obj["config"]
    db_path = cfg["db"]["path"]
    conn = db_module.connect(db_path)
    db_module.init_schema(conn)

    client = RTDSClient(
        url=cfg["ingest"]["rtds_url"],
        max_backoff_seconds=cfg["ingest"]["reconnect_max_backoff_seconds"],
    )

    def _shutdown(*_) -> None:
        client.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run() -> None:
        count = 0
        async for trade in client.trades():
            persist(conn, trade)
            count += 1
            if count % 100 == 0:
                logging.info("persisted %d trades", count)
        logging.info("ingest stopped after %d trades", count)

    try:
        asyncio.run(_run())
    finally:
        conn.close()


if __name__ == "__main__":
    main(prog_name="whitewhale")
