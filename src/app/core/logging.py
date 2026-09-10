"""log_helper.py: Helper for logging with UTC timestamps and colour-coded console output."""

import logging
import os
import sys
import time
from typing import Any

# ANSI colour codes
_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREY = "\033[38;5;240m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RED_BG = "\033[41m"

_LEVEL_COLORS = {
    logging.DEBUG: _GREY,
    logging.INFO: _CYAN,
    logging.WARNING: _YELLOW,
    logging.ERROR: _RED,
    logging.CRITICAL: _RED_BG + _BOLD,
}

_LOG_FORMAT = "%(asctime)s UTC - %(name)s - %(levelname)s - [Line:%(lineno)d] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ExceptionCaptureFilter(logging.Filter):
    """Intercepts ERROR/CRITICAL log records and forwards them to the digest alerter.

    The filter relies on a live ``sys.exc_info()``: every ``logger.error()``
    (or ``logger.exception()``) invoked inside an ``except`` block produces
    one queued entry for the error-digest email, regardless of whether the
    exception propagates back to the HTTP middleware.  This is what lets
    inner failures (model fallback errors, retry-loop failures, fire-and-
    forget asyncio tasks, …) land in the digest instead of being silently
    swallowed once a fallback succeeds.

    Idempotency is enforced inside the alerter: only the FIRST exception
    captured per request/task produces a digest row.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            import sys

            if sys.exc_info()[0] is not None:
                try:
                    from app.observability.error_alerter import capture_exception_for_alerter

                    capture_exception_for_alerter(logger_name=record.name)
                except Exception:
                    pass
        return True


class _ColorFormatter(logging.Formatter):
    """Formatter that applies ANSI colour codes based on log level (console only)."""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelno, _RESET)
        # Colour the level name and message; leave timestamp/name/lineno plain
        record = logging.makeLogRecord(record.__dict__)
        record.levelname = f"{color}{record.levelname}{_RESET}"
        record.msg = f"{color}{record.msg}{_RESET}"
        return super().format(record)


def _supports_color() -> bool:
    """Return True if the current terminal supports ANSI colour codes."""
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


class StructuredLogger:
    """Compatibility wrapper that adds the PPTX helper methods on top of stdlib logging."""

    def __init__(self, base_logger: logging.Logger):
        self._logger = base_logger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)

    def _emit(
        self,
        level: int,
        message: str,
        *,
        detail: str = "",
        stacklevel: int = 2,
    ) -> None:
        msg = str(message)
        if detail:
            msg = f"{msg}  {detail}"
        self._logger.log(level, msg, stacklevel=stacklevel)

    def banner(self, title: str, subtitle: str = "") -> None:
        bar = "=" * max(len(title), len(subtitle), 32)
        self._emit(logging.INFO, bar, stacklevel=3)
        self._emit(logging.INFO, title, stacklevel=3)
        if subtitle:
            self._emit(logging.INFO, subtitle, stacklevel=3)
        self._emit(logging.INFO, bar, stacklevel=3)

    def stage(self, current: int, total: int, name: str, detail: str = "") -> None:
        prefix = f"[{current}/{total}] {name}"
        self._emit(logging.INFO, prefix, detail=detail, stacklevel=3)

    def step(self, msg: str, detail: str = "") -> None:
        self._emit(logging.INFO, msg, detail=detail, stacklevel=3)

    def success(self, msg: str, detail: str = "") -> None:
        self._emit(logging.INFO, msg, detail=detail, stacklevel=3)

    def warn(self, msg: str, *args: Any, detail: str = "", **kwargs: Any) -> None:
        if args or kwargs:
            self._logger.warning(msg, *args, stacklevel=3, **kwargs)
            return
        self._emit(logging.WARNING, msg, detail=detail, stacklevel=3)

    def error(self, msg: str, *args: Any, detail: str = "", **kwargs: Any) -> None:
        if args or kwargs:
            self._logger.error(msg, *args, stacklevel=3, **kwargs)
            return
        self._emit(logging.ERROR, msg, detail=detail, stacklevel=3)

    def info(self, msg: str, *args: Any, detail: str = "", **kwargs: Any) -> None:
        if args or kwargs:
            self._logger.info(msg, *args, stacklevel=3, **kwargs)
            return
        self._emit(logging.INFO, msg, detail=detail, stacklevel=3)

    def kv(self, key: str, value: Any) -> None:
        self._emit(logging.INFO, f"{key}: {value}", stacklevel=3)

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        *,
        title: str = "",
        align: list[str] | None = None,
    ) -> None:
        if not rows:
            return

        col_count = len(headers)
        align = (align or ["l"] * col_count)[:col_count]
        widths = [
            max(len(str(headers[i])), max((len(str(r[i])) for r in rows), default=0))
            for i in range(col_count)
        ]

        def _cell(text: Any, width: int, mode: str) -> str:
            text = str(text)
            if mode == "r":
                return text.rjust(width)
            if mode == "c":
                return text.center(width)
            return text.ljust(width)

        separator = "-" * (sum(widths) + 3 * (col_count - 1))
        if title:
            self._emit(logging.INFO, title, stacklevel=3)
        self._emit(logging.INFO, separator, stacklevel=3)
        self._emit(
            logging.INFO,
            " | ".join(_cell(headers[i], widths[i], align[i]) for i in range(col_count)),
            stacklevel=3,
        )
        self._emit(logging.INFO, separator, stacklevel=3)
        for row in rows:
            self._emit(
                logging.INFO,
                " | ".join(_cell(row[i], widths[i], align[i]) for i in range(col_count)),
                stacklevel=3,
            )
        self._emit(logging.INFO, separator, stacklevel=3)


def setup_logging(file: str | None = None, log_file_path: str = "app.log") -> StructuredLogger:
    """Configure the application logging system with console and file output using UTC time.

    Console output is colour-coded by level when running in a TTY:
        DEBUG    → grey
        INFO     → cyan
        WARNING  → yellow
        ERROR    → red
        CRITICAL → red background + bold

    File output always uses plain text (no ANSI escape codes).
    """
    logger_name = os.path.basename(file) if file else "default_logger"
    logger = logging.getLogger(logger_name)

    if not logger.handlers:  # Avoid adding handlers multiple times
        logger.setLevel(logging.INFO)
        logger.addFilter(_ExceptionCaptureFilter())

        plain_formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        plain_formatter.converter = time.gmtime

        # Console handler — colour when TTY, plain otherwise
        console_handler = logging.StreamHandler(sys.stderr)
        if _supports_color():
            color_formatter = _ColorFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
            color_formatter.converter = time.gmtime
            console_handler.setFormatter(color_formatter)
        else:
            console_handler.setFormatter(plain_formatter)
        logger.addHandler(console_handler)

        # File handler — always plain text
        file_handler = logging.FileHandler(log_file_path, mode="a")
        file_handler.setFormatter(plain_formatter)
        logger.addHandler(file_handler)

        # Prevent log propagation to root logger (avoids duplicate output)
        logger.propagate = False

    return StructuredLogger(logger)
