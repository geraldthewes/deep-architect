from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(log_dir: Path) -> Path:
    """Initialize rich console + file logger. Returns log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"architect-run-{run_id}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True),
            logging.FileHandler(log_file),
        ],
    )
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
