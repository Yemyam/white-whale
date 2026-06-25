"""White Whale command-line interface."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path

import click

from whitewhale import backtest as backtest_module
from whitewhale import config as config_loader
from whitewhale import db as db_module
from whitewhale import health as health_module
from whitewhale import stats as stats_module
from whitewhale.alert import AlertConfig, AlertEmitter
from whitewhale.filter import WhaleConfig, iter_whale_trades
from whitewhale.scoring import ScoringConfig, score_whale_event
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
    ctx.obj["config_path"] = config_path
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
        health_module.write_heartbeat(conn, "ingest", {"trades": count})
        async for trade in client.trades():
            persist(conn, trade)
            count += 1
            if count % 100 == 0:
                logging.info("persisted %d trades", count)
                health_module.write_heartbeat(conn, "ingest", {"trades": count})
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

    # In --loop mode, re-read limit/max_age each cycle so they can be tuned live.
    reloadable = config_loader.ReloadableConfig(ctx.obj["config_path"])

    async def _run() -> None:
        async with GammaClient(
            base_url=gcfg["base_url"],
            timeout_seconds=gcfg["request_timeout_seconds"],
            min_request_interval_seconds=gcfg["min_request_interval_seconds"],
        ) as client:
            cur_limit, cur_max_age = eff_limit, eff_max_age
            while True:
                refreshed = await enrich_pending(
                    conn,
                    client,
                    limit=cur_limit,
                    max_age_seconds=cur_max_age,
                )
                if not continuous:
                    click.echo(f"refreshed {refreshed} markets")
                    return
                health_module.write_heartbeat(conn, "enrich-markets", {"refreshed": refreshed})
                if reloadable.reload_if_changed() and limit is None and max_age_seconds is None:
                    g = reloadable.data["ingest"]["gamma"]
                    cur_limit = g["refresh_batch_limit"]
                    cur_max_age = g["refresh_max_age_seconds"]
                    logging.info("reloaded enrich config: limit=%s max_age=%s", cur_limit, cur_max_age)
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


@main.command("score")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="Only score trades on or after this date (YYYY-MM-DD, UTC).",
)
@click.option("--limit", type=int, default=None, help="Stop after N scored events.")
@click.option("--json", "as_json", is_flag=True, help="Emit the full score object as JSON.")
@click.pass_context
def score(ctx: click.Context, since_iso: str | None, limit: int | None, as_json: bool) -> None:
    """Run whale events through the Phase 3 copy-score engine and print scores.

    End-to-end check of the engine before alert emission (Phase 4): each whale
    event gets a 0-100 total, a confidence label, and its rationale lines.
    Scores read precomputed `wallet_stats`; backfill + a stats refresh make them
    meaningful (an un-backfilled wallet scores on neutral defaults).
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
    sconfig = ScoringConfig.from_config(cfg)

    try:
        count = 0
        for event in iter_whale_trades(conn, wconfig, since_iso=since_norm):
            result = score_whale_event(conn, event, sconfig)
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "tx_hash": event.tx_hash,
                            "wallet": event.wallet,
                            "condition_id": event.condition_id,
                            "size_usdc": event.size_usdc,
                            "total": result.total,
                            "confidence": result.confidence,
                            "components": result.components,
                            "rationale": result.rationale,
                        }
                    )
                )
            else:
                click.echo(
                    f"{event.occurred_at.isoformat()}  {result.total:3d}  "
                    f"{result.confidence:6}  {event.wallet}  ${event.size_usdc:,.0f}"
                )
            count += 1
            if limit is not None and count >= limit:
                break
        click.echo(f"{count} events scored", err=True)
    finally:
        conn.close()


@main.command("alert")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="Only consider trades on or after this date (YYYY-MM-DD, UTC).",
)
@click.option("--limit", type=int, default=None, help="Stop after N events considered.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Score and report what would fire without persisting or dropping files.",
)
@click.pass_context
def alert(ctx: click.Context, since_iso: str | None, limit: int | None, dry_run: bool) -> None:
    """Score whale events and emit alerts (Phase 4): JSON file drop + audit row.

    Walks the same whale events as `score`, then applies the alert gates
    (min score, per-market cooldown, dedupe) and drops a JSON file per alert.
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
    sconfig = ScoringConfig.from_config(cfg)
    aconfig = AlertConfig.from_config(cfg)
    emitter = AlertEmitter(conn, aconfig)

    try:
        considered = emitted = 0
        for event in iter_whale_trades(conn, wconfig, since_iso=since_norm):
            result = score_whale_event(conn, event, sconfig)
            considered += 1
            if dry_run:
                fired = result.total >= aconfig.min_score
                emitted += fired
                click.echo(
                    f"{'WOULD-FIRE' if fired else 'skip      '}  {result.total:3d}  "
                    f"{result.confidence:6}  {event.wallet}  {event.condition_id}"
                )
            else:
                outcome = emitter.emit(event, result)
                if outcome.emitted:
                    emitted += 1
                    click.echo(f"FIRED  {result.total:3d}  {outcome.alert_id}  -> {outcome.path}")
                else:
                    click.echo(f"skip   {result.total:3d}  ({outcome.reason})  {event.condition_id}")
            if limit is not None and considered >= limit:
                break
        click.echo(f"{emitted} alerts from {considered} events considered", err=True)
    finally:
        conn.close()


@main.command("refresh-stats")
@click.option("--wallet", "wallets", multiple=True, help="Refresh only these wallets (repeatable).")
@click.option("--all", "refresh_all", is_flag=True, help="Recompute every wallet, not just stale ones.")
@click.option(
    "--as-of",
    type=str,
    default=None,
    help="Reference time for the 30d windows (ISO 8601, UTC). Defaults to now.",
)
@click.option("--loop", "continuous", is_flag=True, help="Refresh on an interval (daemon mode).")
@click.option(
    "--interval",
    type=int,
    default=None,
    help="Seconds between refreshes in --loop mode. Defaults to stats.refresh_interval_seconds.",
)
@click.pass_context
def refresh_stats(
    ctx: click.Context,
    wallets: tuple[str, ...],
    refresh_all: bool,
    as_of: str | None,
    continuous: bool,
    interval: int | None,
) -> None:
    """Recompute the precomputed `wallet_stats` the score engine reads (Phase 6).

    Cold-path job: scores fall back to neutral defaults until this populates PnL,
    win rate, sizes, and the arb/MM churn signals. Cheap to rerun (only stale
    wallets unless --all). Run daily via the systemd timer, or --loop on a laptop.
    """
    import datetime as _dt
    import time as _time

    cfg = ctx.obj["config"]
    scfg = cfg.get("stats", {})
    rt_window = float(
        cfg.get("scoring", {}).get("thresholds", {}).get("round_trip_window_seconds", 60)
    )
    eff_interval = interval if interval is not None else int(scfg.get("refresh_interval_seconds", 86400))
    as_of_dt = (
        _dt.datetime.fromisoformat(as_of).replace(tzinfo=_dt.timezone.utc) if as_of else None
    )

    conn = db_module.connect(cfg["db"]["path"])
    db_module.init_schema(conn)
    reloadable = config_loader.ReloadableConfig(ctx.obj["config_path"])

    try:
        while True:
            n = stats_module.refresh_wallet_stats(
                conn,
                as_of=as_of_dt,
                wallets=list(wallets) or None,
                only_stale=not refresh_all,
                round_trip_window_seconds=rt_window,
            )
            health_module.write_heartbeat(conn, "refresh-stats", {"wallets_refreshed": n})
            click.echo(f"refreshed {n} wallets")
            if not continuous:
                return
            if reloadable.reload_if_changed():
                rt_window = float(
                    reloadable.data.get("scoring", {})
                    .get("thresholds", {})
                    .get("round_trip_window_seconds", 60)
                )
                if interval is None:
                    eff_interval = int(reloadable.data.get("stats", {}).get("refresh_interval_seconds", 86400))
                logging.info("reloaded refresh-stats config: rt_window=%s interval=%s", rt_window, eff_interval)
            _time.sleep(eff_interval)
    finally:
        conn.close()


@main.command("health")
@click.option("--serve", is_flag=True, help="Run the HTTP health server instead of printing once.")
@click.option("--host", type=str, default=None, help="Bind host (default health.host).")
@click.option("--port", type=int, default=None, help="Bind port (default health.port).")
@click.pass_context
def health(ctx: click.Context, serve: bool, host: str | None, port: int | None) -> None:
    """Report system health (Phase 6): DB counts, freshness, and heartbeats.

    One-shot prints a JSON snapshot and exits non-zero if unhealthy. `--serve`
    runs a stdlib HTTP server answering GET /health (200 healthy, 503 stale).
    """
    cfg = ctx.obj["config"]
    hcfg = cfg.get("health", {})
    stale_after = float(hcfg.get("stale_after_seconds", 900))

    if serve:
        eff_host = host or hcfg.get("host", "127.0.0.1")
        eff_port = port if port is not None else int(hcfg.get("port", 8787))
        click.echo(f"serving health on http://{eff_host}:{eff_port}/health", err=True)
        health_module.serve(
            cfg["db"]["path"], host=eff_host, port=eff_port, stale_after_seconds=stale_after
        )
        return

    conn = db_module.connect(cfg["db"]["path"])
    db_module.init_schema(conn)
    try:
        status = health_module.gather_status(conn, stale_after_seconds=stale_after)
    finally:
        conn.close()
    click.echo(json.dumps(status, indent=2))
    if not status["healthy"]:
        raise SystemExit(1)


@main.command("backtest")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="Only replay trades on or after this date (YYYY-MM-DD, UTC).",
)
@click.option(
    "--holdout-weeks",
    type=float,
    default=6.0,
    help="Hold out the newest N weeks of trades for out-of-sample evaluation.",
)
@click.option(
    "--min-score",
    type=int,
    default=None,
    help="Alert threshold for the 'would-have-alerted' subset. Defaults to alerts.min_score.",
)
@click.option(
    "--optimize",
    is_flag=True,
    help="Re-fit weights to maximize in-sample rank correlation; report OOS.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.pass_context
def backtest(
    ctx: click.Context,
    since_iso: str | None,
    holdout_weeks: float,
    min_score: int | None,
    optimize: bool,
    as_json: bool,
) -> None:
    """Replay resolved-market whale trades and measure copy EV (Phase 5).

    For each historical whale trade that landed in a *resolved* market, computes
    whether copying it to settlement was profitable, then reports how well
    `score.total` ranks the profitable copies. Laptop-only; needs enriched,
    resolved markets and real (non-sentinel) trade outcomes in the DB.
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

    eff_min_score = min_score if min_score is not None else AlertConfig.from_config(cfg).min_score

    conn = db_module.connect(cfg["db"]["path"])
    db_module.init_schema(conn)
    wconfig = WhaleConfig.from_config(cfg)
    sconfig = ScoringConfig.from_config(cfg)

    try:
        samples = backtest_module.collect_samples(
            conn, wconfig, sconfig, since_iso=since_norm, holdout_weeks=holdout_weeks
        )
    finally:
        conn.close()

    if not samples:
        click.echo(
            "0 settled whale trades to backtest. Need enriched + resolved markets and "
            "real trade outcomes (subgraph-only rows carry sentinel outcomes).",
            err=True,
        )
        return

    report = backtest_module.summarize(samples, sconfig.weights, min_score=eff_min_score)
    opt = (
        backtest_module.optimize_weights(samples, sconfig) if optimize else None
    )

    if as_json:
        click.echo(json.dumps(_backtest_json(report, opt), indent=2, default=_json_default))
    else:
        _print_backtest(report, opt)


def _json_default(o: object):
    if isinstance(o, float) and o == float("-inf"):
        return None
    raise TypeError(f"not serializable: {type(o)}")


def _backtest_json(report, opt) -> dict:
    from dataclasses import asdict

    out = {"report": asdict(report)}
    if opt is not None:
        weights, obj_in, obj_out = opt
        out["optimized"] = {
            "weights": weights,
            "objective_in_sample": obj_in,
            "objective_out_of_sample": obj_out if obj_out != float("-inf") else None,
        }
    return out


def _fmt(x: float | None, spec: str = ".3f") -> str:
    return "n/a" if x is None or x == float("-inf") else format(x, spec)


def _print_backtest(report, opt) -> None:
    click.echo(
        f"samples={report.n_samples} (in={report.n_in_sample} out={report.n_out_of_sample})  "
        f"hit_rate={report.hit_rate:.1%}  mean_return={report.mean_return:+.3f}  "
        f"total_pnl=${report.total_pnl_usdc:,.0f}"
    )
    click.echo(
        f"score<->EV  spearman in={_fmt(report.spearman_in)} out={_fmt(report.spearman_out)}  "
        f"pearson in={_fmt(report.pearson_in)} out={_fmt(report.pearson_out)}"
    )
    click.echo(
        f"alerted (>= {report.min_score}): n={report.alerted_n}  "
        f"hit_rate={_fmt(report.alerted_hit_rate, '.1%')}  "
        f"mean_return={_fmt(report.alerted_mean_return, '+.3f')}"
    )
    click.echo("score bucket    n   hit_rate  mean_return     total_pnl")
    for b in report.buckets:
        click.echo(
            f"  [{b.lo:3d},{b.hi:3d})  {b.n:4d}   {b.hit_rate:6.1%}   {b.mean_return:+8.3f}   "
            f"${b.total_pnl_usdc:>12,.0f}"
        )
    if report.cohorts:
        click.echo("cohort                  n   mean_score  median  mean_return  hit_rate")
        for c in report.cohorts:
            click.echo(
                f"  {c.name:20.20}  {c.n:4d}   {c.mean_score:8.1f}  {c.median_score:6.1f}  "
                f"{c.mean_return:+9.3f}  {c.hit_rate:7.1%}"
            )
    if opt is not None:
        weights, obj_in, obj_out = opt
        click.echo(f"optimized weights (in-sample rho={_fmt(obj_in)}, OOS rho={_fmt(obj_out)}):")
        for name, w in weights.items():
            click.echo(f"  {name:26} {w:.3f}")


if __name__ == "__main__":
    main(prog_name="whitewhale")
