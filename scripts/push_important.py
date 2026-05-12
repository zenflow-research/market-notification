"""B5: Discord notifier for newly-completed `important` notifications.

Long-running daemon that polls ``data/notifications.db`` for rows where:

  - ``ai_priority = 'important'``
  - ``gemini_dive_at IS NOT NULL`` (deep-dive completed)
  - ``id > last_pushed_id`` (haven't notified yet)

For each new row, posts a single Discord embed to the configured webhook.
Tracks ``last_pushed_id`` in ``data/push_state.json`` so the pointer
survives crashes and restarts.

Designed to run alongside ``scripts/run_all.py`` and
``scripts/watch_health.py`` as a third long-running operator process.
It owns *no* state in the DB and writes nothing back -- safe to start /
stop / restart at will.

Usage::

    # foreground, alerts to MN_DISCORD_WEBHOOK env var
    python scripts/push_important.py

    # explicit webhook
    python scripts/push_important.py --discord-webhook https://discord.com/api/webhooks/...

    # dry run (logs what it would push; no HTTP calls; doesn't update state)
    python scripts/push_important.py --dry-run

    # one-shot (push pending then exit; cron-style)
    python scripts/push_important.py --once

    # custom poll interval (seconds)
    python scripts/push_important.py --interval 30

CLI exit codes: 0 (clean shutdown), 1 (config error), 2 (no webhook).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import requests
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.db.session import get_session  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "push_state.json"
DEFAULT_INTERVAL_S = 60
DEFAULT_BATCH_LIMIT = 25
UI_BASE_URL = "http://127.0.0.1:8501"

# Discord embed color per priority. Stays in 0xRRGGBB.
_PRIORITY_COLOR = {
    "important": 0xE74C3C,  # red
    "medium": 0xF39C12,     # orange
    "normal": 0x95A5A6,     # grey
    "ignored": 0x7F8C8D,
}


@dataclass
class PushState:
    last_pushed_id: int = 0
    last_run_at: str = ""
    total_pushed: int = 0

    @classmethod
    def load(cls, path: Path) -> "PushState":
        if not path.exists():
            return cls()
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        return cls(
            last_pushed_id=int(blob.get("last_pushed_id", 0)),
            last_run_at=str(blob.get("last_run_at", "")),
            total_pushed=int(blob.get("total_pushed", 0)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)


@dataclass
class NotificationRow:
    id: int
    source: str
    company_id: int
    company_name_raw: str | None
    headline: str
    ai_category: str | None
    ai_priority: str | None
    gemma_impact: str | None
    gemma_summary: str | None
    announced_at: Any  # datetime or string
    gemini_dive_at: Any
    gemini_cache_hit: int | None
    gemini_profile_used: str | None


_FETCH_SQL = """
    SELECT id, source, company_id, company_name_raw, headline, ai_category,
           ai_priority, gemma_impact, gemma_summary, announced_at,
           gemini_dive_at, gemini_cache_hit, gemini_profile_used
      FROM notifications
     WHERE ai_priority = 'important'
       AND gemini_dive_at IS NOT NULL
       AND id > :last_id
     ORDER BY id ASC
     LIMIT :limit
"""


def fetch_new_important(last_id: int, limit: int, log) -> list[NotificationRow]:
    """One-shot DB read of pending important rows. Read-only, no transactions."""
    out: list[NotificationRow] = []
    try:
        with get_session() as sess:
            rows = sess.execute(
                text(_FETCH_SQL),
                {"last_id": last_id, "limit": limit},
            ).all()
            for r in rows:
                out.append(NotificationRow(*r))
    except Exception as exc:  # noqa: BLE001
        log.exception("DB fetch failed: %s", exc)
    return out


def _short(s: str | None, max_len: int) -> str:
    if not s:
        return ""
    s = " ".join(s.split())  # collapse whitespace
    return s if len(s) <= max_len else s[: max_len - 1] + "..."


def _company_label(row: NotificationRow) -> str:
    if row.company_name_raw:
        return row.company_name_raw
    return f"Company {row.company_id}"


def _iso(value: Any) -> str:
    """ISO-format a datetime or pass through if already a string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def build_discord_embed(row: NotificationRow) -> dict[str, Any]:
    """A single Discord embed for one important deep-dived notification."""
    color = _PRIORITY_COLOR.get(row.ai_priority or "important", _PRIORITY_COLOR["important"])
    title = f"{_company_label(row)} -- {_short(row.headline, 200)}"
    description = _short(row.gemma_impact, 1000) or _short(row.gemma_summary, 1000) or "(no summary)"
    fields = [
        {"name": "Source", "value": row.source or "?", "inline": True},
        {"name": "Category", "value": row.ai_category or "?", "inline": True},
        {"name": "id", "value": str(row.id), "inline": True},
    ]
    if row.gemini_profile_used:
        fields.append({
            "name": "Deep-dive",
            "value": f"profile={row.gemini_profile_used} "
                     f"cache={'hit' if row.gemini_cache_hit else 'miss'}",
            "inline": True,
        })
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": description[:2048],
        "url": UI_BASE_URL,
        "color": color,
        "fields": fields,
        "timestamp": _iso(row.announced_at),
        "footer": {"text": "market_notification"},
    }
    return embed


def post_to_discord(webhook_url: str, embeds: list[dict[str, Any]], log) -> bool:
    """Discord accepts up to 10 embeds in one ``content`` POST."""
    if not embeds:
        return True
    payload = {"embeds": embeds[:10]}
    try:
        r = requests.post(webhook_url, json=payload, timeout=15)
    except requests.RequestException as exc:
        log.warning("discord post failed: %s", exc)
        return False
    if r.status_code >= 300:
        log.warning("discord webhook %s: %s", r.status_code, r.text[:200])
        return False
    return True


class Pusher:
    def __init__(
        self,
        webhook_url: str | None,
        state_path: Path,
        interval_s: int,
        batch_limit: int,
        dry_run: bool,
    ) -> None:
        self.webhook_url = webhook_url
        self.state_path = state_path
        self.interval_s = interval_s
        self.batch_limit = batch_limit
        self.dry_run = dry_run
        self.state = PushState.load(state_path)
        self.log = get_logger("scripts.push_important")
        self._stop = threading.Event()

    def request_stop(self, reason: str = "explicit") -> None:
        if not self._stop.is_set():
            self.log.info("stop requested (%s)", reason)
        self._stop.set()

    def tick(self) -> int:
        """Fetch + push pending. Returns count pushed."""
        rows = fetch_new_important(self.state.last_pushed_id, self.batch_limit, self.log)
        if not rows:
            self.log.debug("no new important rows (last_pushed_id=%d)",
                           self.state.last_pushed_id)
            return 0
        self.log.info("found %d new important rows (last_pushed_id=%d -> %d)",
                      len(rows), self.state.last_pushed_id, rows[-1].id)
        embeds = [build_discord_embed(r) for r in rows]

        if self.dry_run:
            for r in rows:
                self.log.info("DRY id=%d source=%s cat=%s headline=%s",
                              r.id, r.source, r.ai_category, _short(r.headline, 80))
            # Don't advance state on dry-run.
            return len(rows)

        if not self.webhook_url:
            self.log.error("no webhook configured; cannot push")
            return 0

        # Post in chunks of 10 embeds each (Discord limit).
        pushed = 0
        for start in range(0, len(embeds), 10):
            chunk = embeds[start:start + 10]
            if not post_to_discord(self.webhook_url, chunk, self.log):
                self.log.warning("aborting batch after %d/%d pushed", pushed, len(rows))
                break
            pushed += len(chunk)
            # Track the highest id we've actually delivered.
            self.state.last_pushed_id = rows[start + len(chunk) - 1].id
            self.state.total_pushed += len(chunk)
            self.state.last_run_at = datetime.now(tz=timezone.utc).isoformat()
            self.state.save(self.state_path)
        return pushed

    def run(self) -> int:
        self.log.info(
            "pusher starting: interval=%ds batch_limit=%d state=%s dry=%s",
            self.interval_s, self.batch_limit, self.state_path, self.dry_run,
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.log.exception("tick raised: %s", exc)
            slept = 0.0
            while slept < self.interval_s and not self._stop.is_set():
                time.sleep(0.5)
                slept += 0.5
        self.log.info("pusher stopped")
        return 0


@click.command()
@click.option("--discord-webhook", default=None,
              help="Discord webhook URL. Falls back to MN_DISCORD_WEBHOOK env var.")
@click.option("--interval", default=DEFAULT_INTERVAL_S, show_default=True, type=int,
              help="Seconds between polls.")
@click.option("--batch-limit", default=DEFAULT_BATCH_LIMIT, show_default=True, type=int,
              help="Max rows fetched per tick.")
@click.option("--state-file", default=str(DEFAULT_STATE_PATH), show_default=True,
              type=click.Path(dir_okay=False),
              help="Where to persist last_pushed_id.")
@click.option("--dry-run", is_flag=True,
              help="Fetch + format + log; do not POST and do not advance state.")
@click.option("--once", is_flag=True,
              help="Fetch + push once, then exit (cron-style).")
def main(
    discord_webhook: str | None,
    interval: int,
    batch_limit: int,
    state_file: str,
    dry_run: bool,
    once: bool,
) -> int:
    configure_logging(process_name="push-important")
    log = get_logger("scripts.push_important")

    # Validate settings load (so a malformed config errors here, not later).
    try:
        _ = get_settings()
    except Exception as exc:  # noqa: BLE001
        log.exception("config load failed: %s", exc)
        sys.exit(1)

    webhook = discord_webhook or os.environ.get("MN_DISCORD_WEBHOOK") or None
    if not webhook and not dry_run:
        log.error(
            "No webhook configured. Either pass --discord-webhook URL, "
            "set MN_DISCORD_WEBHOOK, or use --dry-run."
        )
        sys.exit(2)

    pusher = Pusher(
        webhook_url=webhook,
        state_path=Path(state_file),
        interval_s=interval,
        batch_limit=batch_limit,
        dry_run=dry_run,
    )

    def _on_sigint(_signum, _frame):  # noqa: ARG001
        pusher.request_stop("SIGINT")

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        signal.signal(signal.SIGTERM, _on_sigint)
    except (ValueError, AttributeError):
        pass

    if once:
        pushed = pusher.tick()
        log.info("once mode: pushed %d", pushed)
        return 0

    return pusher.run()


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
