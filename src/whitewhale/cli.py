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
from whitewhale.filter import WhaleConfig, iter_whale_trades
from whitewhale.ingest.gamma import GammaClient, enrich_pending
from whitewhale.ingest.rtds import RTDSClient, persist
from whitewhale.ingest.subgraph import SubgraphClient, backfill_wallet


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


@main.command("enrich-markets")
@click.option("--limit", type=int, default=None, help="Override refresh_batch_limit.")
@click.option(
    "--max-age-seconds",
    type=int,
    default=None,
    help="Override refresh_max_age_seconds. Use 0 to refresh all rows.",
)
@click.option("--loop", "continuous", is_flag=True, help="Loop forever (Phase-6-lite for systemd).")
@click.option("--interval", type=int, default=300, help="Seconds between batches in --loop mode.")
@click.pass_context
def enrich_markets(
    ctx: click.Context,
    limit: int | None,
    max_age_seconds: int | None,
    continuous: bool,
    interval: int,
) -> None:
    """Fetch market metadata from Polymarket Gamma and upsert into SQLite."""
    cfg = ctx.obj["config"]
    db_path = cfg["db"]["path"]
    gcfg = cfg["ingest"]["gamma"]
    eff_limit = limit if limit is not None else gcfg["refresh_batch_limit"]
    eff_max_age = (
        max_age_seconds if max_age_seconds is not None else gcfg["refresh_max_age_seconds"]
    )

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)

    async def _run() -> None:
        async with GammaClient(
            base_url=gcfg["base_url"],
            timeout_seconds=gcfg["request_timeout_seconds"],
            min_request_interval_seconds=gcfg["min_request_interval_seconds"],
        ) as client:
            while True:
                refreshed = await enrich_pending(
                    conn,
                    client,
                    limit=eff_limit,
                    max_age_seconds=eff_max_age,
                )
                if not continuous:
                    click.echo(f"refreshed {refreshed} markets")
                    return
                await asyncio.sleep(interval)

    try:
        asyncio.run(_run())
    finally:
        conn.close()


@main.command("backfill-wallets")
@click.argument("wallets", nargs=-1, required=True)
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="Only fetch fills on or after this date (YYYY-MM-DD, UTC). Omit for full history.",
)
@click.pass_context
def backfill_wallets(ctx: click.Context, wallets: tuple[str, ...], since_iso: str | None) -> None:
    """Backfill historical trades for one or more wallet addresses via the
    Polymarket subgraph. Laptop-only; scp the DB to the Pi after.
    """
    import datetime as _dt  # local to keep top imports tidy

    cfg = ctx.obj["config"]
    db_path = cfg["db"]["path"]
    scfg = cfg["ingest"]["subgraph"]

    since_ts = 0
    if since_iso:
        since_ts = int(
            _dt.datetime.fromisoformat(since_iso)
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)

    async def _run() -> None:
        async with SubgraphClient(
            url=scfg["url"],
            timeout_seconds=scfg["request_timeout_seconds"],
            min_request_interval_seconds=scfg["min_request_interval_seconds"],
        ) as client:
            for w in wallets:
                written = await backfill_wallet(conn, client, w, since_ts=since_ts)
                click.echo(f"{w}: wrote {written} trades")

    try:
        asyncio.run(_run())
    finally:
        conn.close()


@main.command("whales")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="Only scan trades on or after this date (YYYY-MM-DD, UTC).",
)
@click.option("--limit", type=int, default=None, help="Stop after N whale events.")
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per event.")
@click.pass_context
def whales(ctx: click.Context, since_iso: str | None, limit: int | None, as_json: bool) -> None:
    """Scan ingested trades through the Phase 2 whale filter and print events.

    Inspection aid before the score engine (Phase 3) exists: shows exactly which
    trades clear the size + liquidity floors and survive wallet+market dedupe.
    """
    import datetime as _dt  # local to keep top imports tidy

    cfg = ctx.obj["config"]
    since_norm: str | None = None
    if since_iso:
        since_norm = (
            _dt.datetime.fromisoformat(since_iso)
            .replace(tzinfo=_dt.timezone.utc)
            .isoformat()
        )

    conn = db_module.connect(cfg["db"]["path"])
    db_module.init_schema(conn)
    wconfig = WhaleConfig.from_config(cfg)

    try:
        count = 0
        for event in iter_whale_trades(conn, wconfig, since_iso=since_norm):
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "tx_hash": event.tx_hash,
                            "log_index": event.log_index,
                            "occurred_at": event.occurred_at.isoformat(),
                            "wallet": event.wallet,
                            "condition_id": event.condition_id,
                            "side": event.side,
                            "outcome": event.outcome,
                            "price": event.price,
                            "size_usdc": event.size_usdc,
                            "market_liquidity_usdc": event.market_liquidity_usdc,
                        }
                    )
                )
            else:
                click.echo(
                    f"{event.occurred_at.isoformat()}  {event.wallet}  "
                    f"{event.side:4}  ${event.size_usdc:,.0f}  {event.condition_id}"
                )
            count += 1
            if limit is not None and count >= limit:
                break
        click.echo(f"{count} whale events", err=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main(prog_name="whitewhale")
