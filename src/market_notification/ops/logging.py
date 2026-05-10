"""Central logging configuration. Rotating file + stdout handlers.

Usage:
    from market_notification.ops.logging import configure_logging, get_logger
    configure_logging()
    log = get_logger(__name__)
    log.info("hello")
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from market_notification.config.settings import get_settings

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def configure_logging(
    level: int | None = None, force: bool = False
) -> logging.Logger:
    """Configure root logger with rotating file + stdout handlers.

    - File handler: 10MB x 10, all levels at DEBUG when MN_DEBUG=1 else INFO.
    - Stdout handler: WARNING+ unless MN_DEBUG=1 (then INFO+).

    Idempotent: safe to call multiple times unless `force=True` clears handlers.
    """
    global _configured
    settings = get_settings()
    root = logging.getLogger()

    if _configured and not force:
        return root

    if force:
        for h in list(root.handlers):
            root.removeHandler(h)

    log_dir = Path(settings.paths.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "market_notification.log"

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    file_handler.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    root.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    stdout_handler.setLevel(logging.INFO if settings.debug else logging.WARNING)
    root.addHandler(stdout_handler)

    if level is not None:
        root.setLevel(level)
    else:
        root.setLevel(logging.DEBUG if settings.debug else logging.INFO)

    # Quiet down noisy libraries
    for noisy in ("urllib3", "requests", "PIL", "pdfminer", "pdfplumber"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    root.info("Logging configured. log_file=%s debug=%s", log_file, settings.debug)
    return root


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper. Calls configure_logging() if not already done."""
    if not _configured:
        configure_logging()
    return logging.getLogger(name)


# Allow `python -m market_notification.ops.logging --selftest`
def _selftest() -> int:
    configure_logging(force=True)
    log = get_logger("ops.logging.selftest")
    log.debug("debug-line")
    log.info("info-line")
    log.warning("warning-line")
    log.error("error-line")
    print("Logging selftest OK; check log file under configured log_dir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
