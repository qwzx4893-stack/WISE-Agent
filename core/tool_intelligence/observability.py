import os
import time
import uuid
import logging
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar
from functools import wraps

from ..paths import LOGS_DIR

try:
    import structlog
    _STRUCTLOG_AVAILABLE = True
except ImportError:
    _STRUCTLOG_AVAILABLE = False

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
_logger = None


class _StdLoggerAdapter:
    """Fallback logger when structlog is not available."""

    def __init__(self, base):
        self._base = base

    def _format(self, msg, **kwargs):
        if kwargs:
            extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
            return f"{msg} | {extra}"
        return msg

    def info(self, msg, **kwargs):
        self._base.info(self._format(msg, **kwargs))

    def warning(self, msg, **kwargs):
        self._base.warning(self._format(msg, **kwargs))

    def error(self, msg, **kwargs):
        self._base.error(self._format(msg, **kwargs))

    def debug(self, msg, **kwargs):
        self._base.debug(self._format(msg, **kwargs))


def setup_logging(log_file: str = None):
    global _logger

    if log_file is None:
        log_file = str(LOGS_DIR / "agent.log")

    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        handler.setLevel(logging.INFO)
        logging.basicConfig(handlers=[handler], level=logging.INFO)
    except Exception:
        # If we cannot write the log file (e.g. read-only FS), fall back to stderr
        logging.basicConfig(level=logging.INFO)

    if _STRUCTLOG_AVAILABLE:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        _logger = structlog.get_logger()
    else:
        _logger = _StdLoggerAdapter(logging.getLogger("agent_os"))


def get_logger():
    global _logger
    if _logger is None:
        setup_logging()
    return _logger


def set_trace_id(trace_id: str = None):
    if trace_id is None:
        trace_id = str(uuid.uuid4())
    _trace_id_var.set(trace_id)
    return trace_id


def get_trace_id():
    return _trace_id_var.get()


def trace(func):
    """Lightweight trace decorator that supports sync and async callables."""
    import asyncio

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        logger = get_logger()
        trace_id = set_trace_id()
        start = time.time()
        try:
            result = func(*args, **kwargs)
            duration = (time.time() - start) * 1000
            logger.info(f"{func.__name__}_end", trace_id=trace_id, duration_ms=duration, status="success")
            return result
        except Exception as e:
            duration = (time.time() - start) * 1000
            logger.error(f"{func.__name__}_end", trace_id=trace_id, duration_ms=duration, status="error", error=str(e))
            raise

    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        logger = get_logger()
        trace_id = set_trace_id()
        start = time.time()
        try:
            result = await func(*args, **kwargs)
            duration = (time.time() - start) * 1000
            logger.info(f"{func.__name__}_end", trace_id=trace_id, duration_ms=duration, status="success")
            return result
        except Exception as e:
            duration = (time.time() - start) * 1000
            logger.error(f"{func.__name__}_end", trace_id=trace_id, duration_ms=duration, status="error", error=str(e))
            raise

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
