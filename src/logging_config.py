import logging
import json
import os
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

# Context variable to hold the request_id for the current request
_request_id_ctx = ContextVar("request_id", default=None)

class JsonFormatter(logging.Formatter):
    """
    A custom logging formatter that outputs log records as JSON.
    It includes standard logging attributes and any extra attributes
    passed as keyword arguments to the logging call.
    It also automatically adds a 'request_id' if available in the ContextVar.
    """
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(), # Handles msg % args
        }

        # Add request_id if it's set in the ContextVar
        request_id = _request_id_ctx.get()
        if request_id:
            log_entry["request_id"] = request_id

        # Add any extra attributes passed as kwargs to the logging call
        # e.g., logger.info("message", event_type="api_call", duration_ms=123)
        # Exclude standard LogRecord attributes and internal ones
        standard_attrs = {
            'name', 'msg', 'levelname', 'levelno', 'pathname', 'filename', 'lineno', 'funcName',
            'created', 'msecs', 'relativeCreated', 'thread', 'threadName', 'processName',
            'process', 'asctime', 'exc_info', 'exc_text', 'stack_info', 'args', 'kwargs',
            '_stack_info'
        }

        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith('_'):
                log_entry[key] = value

        # Add exception information if present
        if record.exc_info:
            log_entry["exc_info"] = self.formatException(record.exc_info)
        # Add stack information if present
        if record.stack_info:
            log_entry["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(log_entry)

def setup_logging():
    """
    Configures the root logger to use JSON formatting for console output.
    This function should be called once at application startup.
    """
    root_logger = logging.getLogger()
    # Remove all existing handlers to prevent duplicate or default logging formats
    if root_logger.handlers:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    # Set the logging level for the root logger
    root_logger.setLevel(logging.INFO)

    # Create a stream handler for console output
    handler = logging.StreamHandler(sys.stdout)
    # Instantiate the custom JsonFormatter
    formatter = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S%z")
    handler.setFormatter(formatter)

    # Add the configured handler to the root logger
    root_logger.addHandler(handler)

    # Persistent file logs — survive restarts
    log_dir = os.path.join(os.path.dirname(__file__), "data", "logs")
    os.makedirs(log_dir, exist_ok=True)

    # All logs (INFO+) → data/logs/arbitrout.log (10MB, 5 backups)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "arbitrout.log"),
        maxBytes=10_000_000, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)

    # Errors only (WARNING+) → data/logs/errors.log for quick review
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "errors.log"),
        maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.WARNING)
    root_logger.addHandler(error_handler)
