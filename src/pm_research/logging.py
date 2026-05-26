import logging
import sys

import structlog

_REDACT_FIELDS = frozenset(
    {
        "aws_secret_access_key",
        "password",
        "token",
        "api_key",
        "secret",
        "webhook_url",
        "discord_webhook_url",
        "healthchecks_url",
    }
)

_REDACTED = "***"


def _redact_secrets(
    logger: logging.Logger,
    method: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    for key in _REDACT_FIELDS:
        if key in event_dict:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
