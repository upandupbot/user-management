"""Papertrail logging.

Two ways to ship logs:

- **HTTPS (preferred, used on EC2):** `papertrail_log_url` + `papertrail_log_token`,
  the same method and line format as the other Up&Up integrations on the server:

      user-management: 142301512, card_id 1445558588 : Replaced Kate with Robyn (Job Owner)

  Lines are posted from a background thread, so a slow Papertrail never slows
  an Apply down. Only activity lines (the `swap_assignee.activity` logger) and
  warnings/errors from the rest of the app are sent; routine info such as scan
  progress stays local.

- **Syslog (legacy):** `papertrail_host` + `papertrail_port`, kept for the CLI
  and older configs.

Never log passwords, tokens, cookies or full Pipefy/Ziflow responses here.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
from datetime import datetime
from typing import Any, Callable, Optional

ACTIVITY_LOGGER = "swap_assignee.activity"
TIMEZONE = "Africa/Johannesburg"

activity_log = logging.getLogger(ACTIVITY_LOGGER)


def activity(message: str, *args: Any, card_id: Any = None, level: int = logging.INFO) -> None:
    """Record something a person did (sign-in, apply, undo...). Goes to Papertrail.

    `card_id` fills the "card_id <id>" slot of the line, like the other
    integrations, so Papertrail searches by card ID find every tool that
    touched the card.
    """
    activity_log.log(level, message, *args, extra={"card_id": card_id})


def _stamp(created: float) -> str:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.fromtimestamp(created, ZoneInfo(TIMEZONE))
    except Exception:  # noqa: BLE001 - no tz database: fall back to server time
        now = datetime.fromtimestamp(created)
    return f"{now.strftime('%H%M%S')}{now.microsecond // 1000:03d}"


def format_line(identifier: str, record: logging.LogRecord) -> str:
    card_id = getattr(record, "card_id", None)
    card = f"card_id {card_id}" if card_id not in (None, "") else "card_id no card_id found"
    message = record.getMessage()
    if record.levelno >= logging.WARNING:
        message = f"{record.levelname}: {message}"
    if record.exc_info and record.levelno >= logging.ERROR:
        # Keep the error type and message; full tracebacks stay in the local log.
        exc = record.exc_info[1]
        if exc is not None:
            message = f"{message} ({type(exc).__name__}: {exc})"
    return f"{identifier}: {_stamp(record.created)}, {card} : {message}"


class PapertrailHTTPHandler(logging.Handler):
    """Posts each line to Papertrail's HTTPS endpoint from a background thread."""

    def __init__(
        self,
        log_url: str,
        token: str,
        identifier: str,
        *,
        sender: Optional[Callable[[str, str, str], None]] = None,
        max_queue: int = 2000,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.log_url = log_url
        self.token = token
        self.identifier = identifier
        self._send = sender or self._post
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=max_queue)
        self._worker = threading.Thread(target=self._run, name="papertrail-sender", daemon=True)
        self._worker.start()
        self.addFilter(_papertrail_filter)

    @staticmethod
    def _post(url: str, token: str, line: str) -> None:
        import requests

        requests.post(
            url,
            auth=("", token),
            headers={"content-type": "text/plain; charset=utf-8"},
            data=line.encode("utf-8"),
            timeout=10,
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(format_line(self.identifier, record))
        except queue.Full:
            pass  # Papertrail unreachable for a long time: drop rather than grow forever
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _run(self) -> None:
        while True:
            line = self._queue.get()
            try:
                self._send(self.log_url, self.token, line)
            except Exception:  # noqa: BLE001 - logging must never break the app
                pass
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (briefly) until queued lines are sent. Used by tests."""
        done = threading.Event()

        def _wait():
            self._queue.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout)


def _papertrail_filter(record: logging.LogRecord) -> bool:
    """Activity lines at any level, plus warnings and errors from anywhere.
    Routine info (scan progress, previews) stays out of Papertrail."""
    if record.name == ACTIVITY_LOGGER or record.name.startswith(ACTIVITY_LOGGER + "."):
        return True
    return record.levelno >= logging.WARNING


def attach_papertrail(
    token: Optional[str],
    host: Optional[str],
    port: Optional[int],
    *,
    log_url: Optional[str] = None,
    identifier: str = "swap-assignee",
) -> bool:
    """Attach a Papertrail handler to the root logger. Returns True on success.

    HTTPS when `log_url` and `token` are set (preferred); otherwise syslog when
    `host` and `port` are set. Attaching twice is a no-op.
    """
    root = logging.getLogger()
    if any(isinstance(h, PapertrailHTTPHandler) for h in root.handlers):
        return True
    activity_log.setLevel(logging.INFO)
    if log_url and token:
        try:
            root.addHandler(PapertrailHTTPHandler(log_url, token, identifier))
            return True
        except Exception:  # noqa: BLE001
            return False
    if not (token and host and port):
        return False
    try:
        handler = logging.handlers.SysLogHandler(address=(host, int(port)))
        fmt = logging.Formatter("%(asctime)s swap-assignee: %(levelname)s %(name)s %(message)s")
        handler.setFormatter(fmt)
        root.addHandler(handler)
        return True
    except Exception:
        return False
