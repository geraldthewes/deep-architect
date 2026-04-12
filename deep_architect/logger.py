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

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    rich_handler = RichHandler(console=console, rich_tracebacks=True)
    rich_handler.setLevel(logging.INFO)
    rich_handler.setFormatter(logging.Formatter("%(message)s"))

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))

    root.addHandler(rich_handler)
    root.addHandler(file_handler)

    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
