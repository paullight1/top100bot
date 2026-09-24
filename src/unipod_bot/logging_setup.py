from __future__ import annotations

import logging
import sys


def configure_logging(*, level: int, debug: bool) -> None:
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    if debug:
        fmt = "%(asctime)s %(levelname)s [%(name)s] %(filename)s:%(lineno)d %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("unipod_bot").setLevel(level)
    if not debug:
        logging.getLogger("neonize").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
