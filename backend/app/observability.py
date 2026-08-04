"""Structured logging helpers.

One format everywhere: ``event_name key=value key=value``, matching the
``api_request method=... path=...`` lines the request middleware already emits.
That keeps the journal greppable — ``journalctl -u … | grep external_call`` gives
every outbound API call, ``grep block_resolved`` every report section.

Two things are deliberate:

* **Durations are always milliseconds**, named ``duration_ms``, so one grep finds
  slow anything regardless of which subsystem produced it.
* **Nothing here takes a credential.** Callers pass service and operation names,
  never tokens or URLs with keys in them — a log line is the easiest place to
  leak a secret and the hardest place to notice you have.
"""

from __future__ import annotations

import typing

import logging
import time
from contextlib import contextmanager


def _format_value(value: typing.Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    text = str(value)
    if not text:
        return "-"
    # Keep one event on one greppable line, and keep spaces from splitting a pair.
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) > 200:
        text = text[:197] + "..."
    return f'"{text}"' if " " in text else text


def format_event(event: str, **fields: typing.Any) -> str:
    """``event key=value key=value`` — the shape every log line in the app takes."""
    parts = [event]
    for key, value in fields.items():
        parts.append(f"{key}={_format_value(value)}")
    return " ".join(parts)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: typing.Any,
) -> None:
    logger.log(level, "%s", format_event(event, **fields))


@contextmanager
def timed(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: typing.Any,
) -> typing.Iterator[dict[str, typing.Any]]:
    """Time a block and log exactly one line when it finishes, pass or fail.

    Yields a dict the caller can add fields to as it learns them (a row count, a
    resolved id), so the outcome and its context land on the same line instead of
    being scattered across several.

    An exception is logged with ``outcome=error`` and re-raised — the caller's own
    error handling is untouched, it just no longer happens silently.
    """
    extra: dict[str, typing.Any] = {}
    started = time.perf_counter()
    try:
        yield extra
    except Exception as error:
        log_event(
            logger,
            event,
            level=logging.ERROR,
            outcome="error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_type=type(error).__name__,
            error=str(error)[:200],
            **fields,
            **extra,
        )
        raise
    log_event(
        logger,
        event,
        level=level,
        outcome="ok",
        duration_ms=round((time.perf_counter() - started) * 1000),
        **fields,
        **extra,
    )


@contextmanager
def external_call(
    logger: logging.Logger, service: str, operation: str, **fields: typing.Any
) -> typing.Iterator[dict[str, typing.Any]]:
    """One line per outbound third-party request (Ahrefs, Sheets, SE Ranking, …).

    These are the calls that fail in production and the ones nothing was
    recording, so a report section could come back empty with no trace of why.
    """
    with timed(logger, "external_call", service=service, operation=operation, **fields) as extra:
        yield extra
