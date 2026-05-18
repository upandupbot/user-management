"""PaperTrail syslog logger setup."""
from __future__ import annotations

import logging
import logging.handlers
from typing import Optional


def attach_papertrail(token: Optional[str], host: Optional[str], port: Optional[int]) -> bool:
    """Attach a SysLogHandler to the root logger if all params are set. Returns True on success."""
    if not (token and host and port):
        return False
    try:
        handler = logging.handlers.SysLogHandler(address=(host, int(port)))
        fmt = logging.Formatter("%(asctime)s swap-assignee: %(levelname)s %(name)s %(message)s")
        handler.setFormatter(fmt)
        logging.getLogger().addHandler(handler)
        return True
    except Exception:
        return False
