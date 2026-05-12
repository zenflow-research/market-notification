"""B3: atomic backup of ``data/notifications.db`` with retention + optional S3 upload.

Why a script (not just ``cp``):
  - The DB is in WAL mode and being written to by the worker. A naive ``cp``
    can land halfway through a transaction and produce a corrupt snapshot.
  - SQLite's online backup API (``conn.backup()``) is the safe primitive --
    it copies pages atomically while the source is being written.
  - After the backup we ``VACUUM INTO`` is *not* what we want here -- it
    rewrites the source. Instead, the in-process ``conn.backup(target)``
    over a fresh file gives a consistent point-in-time snapshot without
    touching the source.

Usage::

    # Daily snapshot to ./backups/, keep last 14
    python scripts/backup_db.py

    # Custom output dir and retention
    python scripts/backup_db.py --output-dir D:/mn-backups --keep 30

    # Also push to S3 (requires awscli on PATH and credentials configured)
    python scripts/backup_db.py --s3-bucket zenflow-data --s3-prefix backups/mn

    # Smoke / dry run -- doesn't actually write or upload
    python scripts/backup_db.py --dry-run

Exit code 0 on success, 1 on any failure.

Cron-able: register as a Windows Task Scheduler job for daily execution
(see ``docs/SHADOW_MODE.md`` § 7).
"""
from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "backups"
DEFAULT_KEEP = 14


def _resolve_db_path() -> Path:
    """Resolve the live notifications.db from settings."""
    settings = get_settings()
    url = settings.db.url
    # Accept "sqlite:///relative" and "sqlite:///absolute" + "sqlite:///data/..."
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///"):]
        p = Path(raw)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p
    raise ValueError(f"Unsupported db.url for backup: {url}")


def _checkpoint_wal(src: Path, log: logging.Logger) -> None:
    """Best-effort WAL checkpoint to minimize backup size + ensure freshness."""
    try:
        conn = sqlite3.connect(str(src), timeout=10)
        try:
            cur = conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            row = cur.fetchone()
            log.info("wal_checkpoint(PASSIVE) -> %s", row)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        log.warning("WAL checkpoint failed (%s); continuing -- backup may include WAL", exc)


def _backup_with_sqlite_api(src: Path, dst_uncompressed: Path, log: logging.Logger) -> None:
    """Use SQLite's online backup API to produce a consistent snapshot.

    ``conn.backup(target)`` copies pages atomically; safe with the source
    being written. The destination file is closed before we gzip it.
    """
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    dst_conn = sqlite3.connect(str(dst_uncompressed))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    log.info("snapshot written: %s (%.2f GB)",
             dst_uncompressed, dst_uncompressed.stat().st_size / (1024 ** 3))


def _gzip_in_place(uncompressed: Path, log: logging.Logger) -> Path:
    """Gzip ``uncompressed`` into ``uncompressed.with_suffix('.db.gz')``.

    Streams in 4 MB chunks to keep memory bounded for multi-GB files.
    Deletes the uncompressed file after a successful gzip.
    """
    gz_path = uncompressed.with_suffix(uncompressed.suffix + ".gz")
    chunk = 4 * 1024 * 1024
    t0 = time.time()
    with open(uncompressed, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
        while True:
            buf = fin.read(chunk)
            if not buf:
                break
            fout.write(buf)
    elapsed = time.time() - t0
    size_in = uncompressed.stat().st_size
    size_out = gz_path.stat().st_size
    ratio = size_out / size_in if size_in else 0.0
    log.info("gzipped %s -> %s (%.2f GB -> %.2f GB, ratio %.2f, %.1f s)",
             uncompressed.name, gz_path.name,
             size_in / (1024 ** 3), size_out / (1024 ** 3),
             ratio, elapsed)
    uncompressed.unlink()
    return gz_path


def _prune_old(output_dir: Path, keep: int, log: logging.Logger) -> int:
    """Delete oldest .db.gz snapshots beyond ``keep`` count. Returns pruned count."""
    snaps = sorted(output_dir.glob("notifications-*.db.gz"))
    to_delete = snaps[:-keep] if keep > 0 else []
    for old in to_delete:
        try:
            old.unlink()
            log.info("pruned old snapshot: %s", old.name)
        except OSError as exc:
            log.warning("could not prune %s: %s", old.name, exc)
    return len(to_delete)


def _s3_upload(gz_path: Path, bucket: str, prefix: str, log: logging.Logger) -> bool:
    """Best-effort S3 upload via the AWS CLI on PATH. Returns True on success."""
    if not bucket:
        return False
    key = f"{prefix.rstrip('/')}/{gz_path.name}"
    s3_uri = f"s3://{bucket}/{key}"
    log.info("uploading to %s ...", s3_uri)
    try:
        proc = subprocess.run(
            ["aws", "s3", "cp", str(gz_path), s3_uri],
            capture_output=True, text=True, timeout=900,
        )
    except FileNotFoundError:
        log.warning("aws CLI not on PATH; skipping S3 upload")
        return False
    except subprocess.TimeoutExpired:
        log.error("S3 upload timed out after 15 min")
        return False
    if proc.returncode != 0:
        log.error("S3 upload failed rc=%d: %s",
                  proc.returncode, (proc.stderr or "")[:400])
        return False
    log.info("S3 upload OK: %s", s3_uri)
    return True


def _timestamped_name(prefix: str = "notifications") -> str:
    """``notifications-YYYYMMDD-HHMMSSZ.db`` -- sortable lexicographically."""
    now = datetime.now(tz=timezone.utc)
    return f"{prefix}-{now.strftime('%Y%m%d-%H%M%SZ')}.db"


@click.command()
@click.option("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
              show_default=True, type=click.Path(file_okay=False))
@click.option("--keep", default=DEFAULT_KEEP, show_default=True, type=int,
              help="Number of most-recent snapshots to keep locally. 0 disables pruning.")
@click.option("--s3-bucket", default=None,
              help="S3 bucket for off-machine retention. Skipped if not set.")
@click.option("--s3-prefix", default="backups/mn-notifications",
              show_default=True, help="Key prefix inside the bucket.")
@click.option("--dry-run", is_flag=True,
              help="Validate source DB readable + paths writable; do not snapshot.")
def main(output_dir: str, keep: int, s3_bucket: str | None,
         s3_prefix: str, dry_run: bool) -> int:
    configure_logging(process_name="backup")
    log = get_logger("scripts.backup_db")

    src = _resolve_db_path()
    if not src.exists():
        log.error("source DB missing: %s", src)
        sys.exit(1)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not out_dir.is_dir():
        log.error("output_dir is not a directory: %s", out_dir)
        sys.exit(1)

    snap_name = _timestamped_name()
    snap_path_db = out_dir / snap_name

    if dry_run:
        free_gb = shutil.disk_usage(str(out_dir)).free / (1024 ** 3)
        src_gb = src.stat().st_size / (1024 ** 3)
        log.info(
            "DRY RUN: src=%s (%.2f GB), dst=%s, free=%.1f GB, keep=%d, s3=%s",
            src, src_gb, snap_path_db, free_gb, keep, s3_bucket or "(none)",
        )
        if free_gb < src_gb * 1.5:
            log.warning("Free space (%.1f GB) < 1.5 x source size (%.2f GB); risky",
                        free_gb, src_gb)
        return 0

    log.info("backing up: %s -> %s", src, snap_path_db)
    _checkpoint_wal(src, log)
    try:
        _backup_with_sqlite_api(src, snap_path_db, log)
    except Exception as exc:  # noqa: BLE001
        log.exception("backup failed: %s", exc)
        # Don't leave a partial file lying around.
        if snap_path_db.exists():
            try:
                snap_path_db.unlink()
            except OSError:
                pass
        sys.exit(1)

    try:
        gz_path = _gzip_in_place(snap_path_db, log)
    except Exception as exc:  # noqa: BLE001
        log.exception("gzip failed: %s", exc)
        sys.exit(1)

    if s3_bucket:
        _s3_upload(gz_path, s3_bucket, s3_prefix, log)

    pruned = _prune_old(out_dir, keep, log)
    log.info("done. retained=%d pruned=%d output=%s",
             keep, pruned, gz_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
