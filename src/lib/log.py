from __future__ import annotations
import logging
import logging.handlers


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                "/state/agent.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )
