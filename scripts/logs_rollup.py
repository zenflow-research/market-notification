"""B4: daily-or-windowed digest of ``logs/*.log*`` files.

Parses the rotating log files emitted by ``ops/logging.py`` (format:
``%(asctime)s %(levelname)-7s %(process_name)s %(name)s %(message)s``)
and produces a Markdown report with:

  - file inventory (scanned vs skipped)
  - level counts per process
  - top-N error patterns (similar errors grouped via a canonicalised hash)
  - top-N warning patterns
  - last 5 raw ERROR records per process for forensic context

Multi-line records (tracebacks) are folded into the preceding record so
the patterns reflect real failure modes, not per-line noise.

Throughput stats live in ``pipeline_journal`` -- not the logs -- so this
script intentionally stays out of that lane (the Streamlit Health tab
covers it).

Usage::

    # Default: last 24h, all logs/*.log*, write logs/rollup-YYYY-MM-DD.md
    python scripts/logs_rollup.py

    # Custom window + output
    python scripts/logs_rollup.py --hours 6 --output logs/rollup-6h.md

    # Print to stdout instead of writing a file
    python scripts/logs_rollup.py --stdout

Exit code 0 on success, 1 on parse-time failure.
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


# Matches the start of a record under ops/logging.py's _FORMAT.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<proc>\S+)\s+"
    r"(?P<logger>\S+)\s+"
    r"(?P<msg>.*)$"
)

# Generic noisy substrings to canonicalise before hashing patterns. We
# squash IDs / timestamps / paths / hex blobs so similar errors fold
# into the same bucket.
_CANON_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<TS>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\bnotif(?:ication)?[_ ]?id[=: ]\s*\d+", re.IGNORECASE), "notif_id=<N>"),
    (re.compile(r"\bpid[=: ]\s*\d+", re.IGNORECASE), "pid=<N>"),
    (re.compile(r"\bid[=: ]\s*\d+", re.IGNORECASE), "id=<N>"),
    (re.compile(r"\b\d{4,}\b"), "<N>"),
    (re.compile(r"[A-Z]:\\[^\s'\"]+"), "<PATH>"),
    (re.compile(r"/[^\s'\"]+"), "<PATH>"),
    (re.compile(r"\s+"), " "),
]


@dataclass
class LogRecord:
    ts: datetime
    level: str
    process: str
    logger: str
    msg: str  # may span multiple lines (continuation appended with \n)
    source_file: str

    def canonical(self) -> str:
        s = self.msg
        for pat, repl in _CANON_RULES:
            s = pat.sub(repl, s)
        return s.strip()[:240]


def _parse_log_file(path: Path, cutoff: datetime) -> list[LogRecord]:
    """Return records with ts >= cutoff. Folds continuation lines into prior record."""
    out: list[LogRecord] = []
    current: LogRecord | None = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                m = _LINE_RE.match(line)
                if m:
                    if current is not None and current.ts >= cutoff:
                        out.append(current)
                    try:
                        ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        current = None
                        continue
                    current = LogRecord(
                        ts=ts,
                        level=m["level"],
                        process=m["proc"],
                        logger=m["logger"],
                        msg=m["msg"],
                        source_file=path.name,
                    )
                else:
                    # Continuation (traceback line); append if current is in window.
                    if current is not None and current.ts >= cutoff:
                        current.msg += "\n" + line
            if current is not None and current.ts >= cutoff:
                out.append(current)
    except OSError:
        pass
    return out


def _short_msg(s: str, max_len: int = 100) -> str:
    s = s.splitlines()[0]
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _hash_canonical(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


def _render_report(
    *,
    files_scanned: list[Path],
    files_skipped: list[Path],
    records: list[LogRecord],
    window_start: datetime,
    window_end: datetime,
) -> str:
    by_level: Counter[str] = Counter(r.level for r in records)
    per_proc_level: dict[str, Counter[str]] = defaultdict(Counter)
    for r in records:
        per_proc_level[r.process][r.level] += 1

    errors = [r for r in records if r.level == "ERROR"]
    warns = [r for r in records if r.level == "WARNING"]

    err_patterns: Counter[str] = Counter()
    err_samples: dict[str, LogRecord] = {}
    for r in errors:
        c = r.canonical()
        err_patterns[c] += 1
        err_samples.setdefault(c, r)

    warn_patterns: Counter[str] = Counter()
    warn_samples: dict[str, LogRecord] = {}
    for r in warns:
        c = r.canonical()
        warn_patterns[c] += 1
        warn_samples.setdefault(c, r)

    lines: list[str] = []
    title_date = window_end.astimezone(timezone.utc).strftime("%Y-%m-%d")
    span_h = (window_end - window_start).total_seconds() / 3600
    lines.append(f"# mn logs rollup -- {title_date} ({span_h:.0f}h window)")
    lines.append("")
    lines.append(f"- **Window**: {window_start.isoformat()} -> {window_end.isoformat()}")
    lines.append(f"- **Files scanned**: {len(files_scanned)}"
                 f" ({', '.join(p.name for p in files_scanned) or '(none)'})")
    if files_skipped:
        lines.append(f"- **Files skipped (no records in window)**: "
                     f"{', '.join(p.name for p in files_skipped)}")
    lines.append(f"- **Total records in window**: {len(records):,}")
    if by_level:
        lines.append("- **By level**: "
                     + " | ".join(f"{lv}: {by_level[lv]:,}" for lv in
                                   ("ERROR", "WARNING", "INFO", "DEBUG", "CRITICAL")
                                   if by_level[lv]))
    lines.append("")

    lines.append("## Per-process level counts")
    lines.append("")
    if not per_proc_level:
        lines.append("_(no records)_")
    else:
        lines.append("| Process | ERROR | WARNING | INFO | DEBUG |")
        lines.append("|---|---:|---:|---:|---:|")
        for proc in sorted(per_proc_level):
            c = per_proc_level[proc]
            lines.append(f"| `{proc}` | {c['ERROR']:,} | {c['WARNING']:,} | "
                         f"{c['INFO']:,} | {c['DEBUG']:,} |")
    lines.append("")

    lines.append("## Top error patterns")
    lines.append("")
    if not err_patterns:
        lines.append("_(no ERROR records in window)_")
    else:
        lines.append("| Count | Hash | Pattern | First seen |")
        lines.append("|---:|---|---|---|")
        for canon, count in err_patterns.most_common(10):
            sample = err_samples[canon]
            lines.append(
                f"| {count} | `{_hash_canonical(canon)}` | "
                f"`{_short_msg(canon)}` | "
                f"{sample.ts.strftime('%Y-%m-%d %H:%M:%SZ')} ({sample.process}) |"
            )
    lines.append("")

    lines.append("## Top warning patterns")
    lines.append("")
    if not warn_patterns:
        lines.append("_(no WARNING records in window)_")
    else:
        lines.append("| Count | Hash | Pattern | First seen |")
        lines.append("|---:|---|---|---|")
        for canon, count in warn_patterns.most_common(10):
            sample = warn_samples[canon]
            lines.append(
                f"| {count} | `{_hash_canonical(canon)}` | "
                f"`{_short_msg(canon)}` | "
                f"{sample.ts.strftime('%Y-%m-%d %H:%M:%SZ')} ({sample.process}) |"
            )
    lines.append("")

    if errors:
        lines.append("## Last 5 ERROR records per process (raw)")
        lines.append("")
        by_proc: dict[str, list[LogRecord]] = defaultdict(list)
        for r in errors:
            by_proc[r.process].append(r)
        for proc in sorted(by_proc):
            recent = sorted(by_proc[proc], key=lambda r: r.ts, reverse=True)[:5]
            lines.append(f"### `{proc}` (last {len(recent)})")
            lines.append("")
            for r in recent:
                lines.append(f"**{r.ts.strftime('%Y-%m-%d %H:%M:%SZ')}** `{r.logger}`")
                lines.append("```")
                lines.append(r.msg)
                lines.append("```")
                lines.append("")

    return "\n".join(lines)


def _collect_log_files(log_dir: Path) -> list[Path]:
    """All .log + rotated .log.N files. Sorted for stable scanning order."""
    files = sorted(log_dir.glob("*.log"))
    files += sorted(log_dir.glob("*.log.*"))
    return files


@click.command()
@click.option("--hours", default=24, show_default=True, type=int,
              help="Window size in hours.")
@click.option("--log-dir", default=None,
              help="Override the configured logs/ directory.")
@click.option("--output", default=None,
              help="Output path (default: logs/rollup-YYYY-MM-DD.md).")
@click.option("--stdout", is_flag=True,
              help="Print to stdout instead of writing a file.")
def main(hours: int, log_dir: str | None, output: str | None, stdout: bool) -> int:
    configure_logging(process_name="logs-rollup")
    log = get_logger("scripts.logs_rollup")

    settings = get_settings()
    dir_path = Path(log_dir) if log_dir else Path(settings.paths.log_dir)
    if not dir_path.exists():
        log.error("log dir missing: %s", dir_path)
        sys.exit(1)

    files = _collect_log_files(dir_path)
    if not files:
        log.warning("no log files in %s", dir_path)

    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    cutoff = now - timedelta(hours=hours)
    log.info("rolling up: dir=%s files=%d window=%dh cutoff=%s",
             dir_path, len(files), hours, cutoff.isoformat())

    all_records: list[LogRecord] = []
    scanned: list[Path] = []
    skipped: list[Path] = []
    for fp in files:
        recs = _parse_log_file(fp, cutoff)
        if recs:
            all_records.extend(recs)
            scanned.append(fp)
        else:
            skipped.append(fp)

    report = _render_report(
        files_scanned=scanned,
        files_skipped=skipped,
        records=all_records,
        window_start=cutoff,
        window_end=now,
    )

    if stdout:
        print(report)
        return 0

    out_path = Path(output) if output else (
        dir_path / f"rollup-{now.strftime('%Y-%m-%d')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    log.info("wrote: %s (%d records, %d ERROR, %d WARN)",
             out_path,
             len(all_records),
             sum(1 for r in all_records if r.level == "ERROR"),
             sum(1 for r in all_records if r.level == "WARNING"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
