"""
utils/logger.py — централизованная настройка логирования.

Принцип: один вызов setup_logging() при старте, далее везде только
  logger = logging.getLogger("bot.module_name")

Никаких print() в кодовой базе. Ruff-правило T201 это ловит.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False


def setup_logging(log_level: str = "INFO", log_file: str | None = None) -> None:
    """
    Настраивает корневой логгер.

    - Всегда выводит в stdout (StreamHandler).
    - Если log_file указан — дополнительно пишет в RotatingFileHandler.
    - Если structlog установлен — использует его для структурированного вывода.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt   = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
    ]

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
        )

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)

    # Заглушаем слишком шумные библиотеки
    logging.getLogger("grpc").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    if _HAS_STRUCTLOG:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
                structlog.dev.ConsoleRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
