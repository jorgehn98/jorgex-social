import json
import logging
import sys

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line on stdout. Never log Meta payloads or third-party text."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


# Paths whose last segment is a credential: the one-use upload URLs of remote agents.
SECRET_PATHS = ("/upload/",)


def _redacted(target: str) -> str:
    path = target.partition("?")[0]
    for prefix in SECRET_PATHS:
        if path.startswith(prefix):
            return prefix + "[redacted]"
    return path


class NoQueryString(logging.Filter):
    """Drops the query string from uvicorn's access log line, which otherwise prints it whole:
    Meta's webhook check carries the verify token there (`hub.verify_token`). Paths stay, except
    the token of an upload URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs '%s - "%s %s HTTP/%s" %d' with (client, method, path?query, version, status).
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], _redacted(args[2]), *args[3:])
        return True


def hide_query_strings() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, NoQueryString) for f in access.filters):
        access.addFilter(NoQueryString())


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    hide_query_strings()
