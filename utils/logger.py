"""
Structured logging for the trading bot.

Provides a configured logger with:
- Console output (INFO+ by default)
- Rotating file output (DEBUG+)
- Structured format with timestamps and module names
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config


def setup_logger(
    name: str = "kalshi_bot",
    log_level: str | None = None,
    log_file: str | None = None,
) -> logging.Logger:
    """
    Create and configure a logger instance.

    Args:
        name: Logger name (used as prefix in log messages).
        log_level: Override log level (defaults to config.LOG_LEVEL).
        log_file: Override log file path (defaults to config.LOG_FILE).

    Returns:
        Configured logging.Logger instance.
    """
    level = getattr(logging, (log_level or config.LOG_LEVEL).upper(), logging.INFO)
    file_path = log_file or config.LOG_FILE

    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)  # Capture everything; handlers filter

    # --- Formatter ---
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s.%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- Console Handler ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # --- File Handler ---
    log_path = Path(file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# Module-level convenience logger
logger = setup_logger()


def get_logger(module_name: str) -> logging.Logger:
    """
    Get a child logger for a specific module.

    Args:
        module_name: The module name (e.g. 'kalshi.client').

    Returns:
        A child logger that inherits the root bot logger's handlers.
    """
    return setup_logger(f"kalshi_bot.{module_name}")
