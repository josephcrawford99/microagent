from __future__ import annotations
import logging
import logging.handlers

from lib.paths import STATE_DIR


def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                str(STATE_DIR / "agent.log"),
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )
