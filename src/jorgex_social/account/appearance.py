"""How this installation looks: its product name, its own CSS and its icon. Kept in the settings
table, so a database backup carries them. One key each, so a page that only needs the name does not
load the CSS and the icon. The web interface and the API both write through `update`, so they share
every limit."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from jorgex_social.account import audit
from jorgex_social.account.models import ActorType, Setting

DEFAULT_NAME = "Social"
MAX_NAME_CHARS = 40
MAX_CSS_BYTES = 256 * 1024
MAX_ICON_BYTES = 256 * 1024
PNG = "image/png"
ICO = "image/x-icon"
SVG = "image/svg+xml"
_KEYS = {
    "name": "appearance.name",
    "name_highlight": "appearance.name_highlight",
    "css": "appearance.css",
    "icon": "appearance.icon",
}
TOO_LARGE_ICON = f"The icon is over {MAX_ICON_BYTES // 1024} KB."
_STYLESHEET = (Path(__file__).parent.parent / "ui" / "static" / "style.css").read_text()
_ROOT = re.search(r"^:root \{.*?^\}", _STYLESHEET, re.M | re.S)[0]
_DECLARATION = r"(?<![\w-])(--[\w-]+)\s*:"
# What an installation's CSS can override, with the defaults: read from the stylesheet, so this
# cannot drift from it.
VARIABLES = dict(re.findall(r"^\s*(--[\w-]+):\s*(.+?);$", _ROOT, re.M))
# Includes what a component sets for itself (a pill's --tone), which is not in the list above but
# does work when overridden.
_DECLARED = set(re.findall(_DECLARATION, _STYLESHEET))
LIGHT_THEME = """\
:root {
  color-scheme: light;
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-raised: #f1f5f9;
  --surface-muted: #f1f5f9;
  --border-subtle: #e2e8f0;
  --border-strong: #cbd5e1;
  --fg-1: #0f172a;
  --fg-2: #334155;
  --fg-muted: #475569;
  --accent: #0e7490;
  --accent-hover: #155e75;
  --accent-2: #6d28d9;
  --on-accent: #ffffff;
  --danger: #b91c1c;
  --warning: #92400e;
  --success: #047857;
  --shadow: #64748b;
}
"""


class InvalidAppearance(ValueError):
    """The message is written for the operator."""


@dataclass(frozen=True)
class Name:
    text: str
    highlight: str

    def parts(self) -> tuple[str, str, str]:
        """Before, highlighted and after, for the template to escape each one on its own."""
        return self.text.partition(self.highlight) if self.highlight else (self.text, "", "")


DEFAULT = Name(DEFAULT_NAME, "")


@dataclass(frozen=True)
class Icon:
    type: str
    data: bytes


def _is_svg(data: bytes) -> bool:
    """Whether the first element is <svg>, after an optional declaration, comments and doctype. No
    XML parser: nothing here needs the document to be well formed, a browser only ever draws it."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    position = len(text) - len(text.lstrip())
    while text.startswith(("<?", "<!"), position):
        closing = "-->" if text.startswith("<!--", position) else ">"
        end = text.find(closing, position)
        if end < 0:
            return False
        position = end + len(closing)
        while position < len(text) and text[position].isspace():
            position += 1
    after = text[position + 4 : position + 5]
    return text.startswith("<svg", position) and (after.isspace() or after in {">", "/"})


def icon_type(data: bytes) -> str:
    """Decided by the content alone: what a client says a file is proves nothing."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    # Reserved zero, type 1 (an icon), and at least one image.
    if data.startswith(b"\x00\x00\x01\x00") and data[4:6] not in {b"", b"\x00", b"\x00\x00"}:
        return ICO
    if _is_svg(data):
        return SVG
    raise InvalidAppearance("The icon must be a PNG, ICO or SVG file.")


def decode_icon(encoded: str) -> bytes:
    if len(encoded) > (MAX_ICON_BYTES // 3 + 1) * 4:  # refused before decoding it
        raise InvalidAppearance(TOO_LARGE_ICON)
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise InvalidAppearance("The icon is not valid base64.") from None


def warnings(css: str) -> list[str]:
    """Custom properties the CSS declares that this interface does not read. Likely typos, but legal
    (an operator may define their own), and a browser ignores them, so they never block a save."""
    # An unclosed comment runs to the end of the file, as it does in a browser.
    declared = re.findall(_DECLARATION, re.sub(r"/\*.*?(?:\*/|\Z)", "", css, flags=re.S))
    return [
        f"{name} is not a variable of this interface and does nothing by itself. "
        "Check it for a typo; if it is your own variable, ignore this."
        for name in dict.fromkeys(declared)
        if name not in _DECLARED
    ]


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= MAX_NAME_CHARS:
        raise InvalidAppearance(f"The name must be 1 to {MAX_NAME_CHARS} characters.")
    return cleaned


def _clean_css(css: str) -> str | None:
    # A browser's textarea sends CRLF: without this, every save would count as a change.
    cleaned = css.replace("\r\n", "\n")
    if len(cleaned.encode()) > MAX_CSS_BYTES:
        raise InvalidAppearance(f"The CSS is over {MAX_CSS_BYTES // 1024} KB.")
    return cleaned or None


def _clean_icon(data: bytes) -> dict[str, str]:
    if len(data) > MAX_ICON_BYTES:
        raise InvalidAppearance(TOO_LARGE_ICON)
    return {"type": icon_type(data), "base64": base64.b64encode(data).decode()}


def _put(session: Session, key: str, value: Any, now: datetime) -> bool:
    """None removes the row, which is what the default looks like. Returns whether it changed."""
    current = session.get(Setting, key)
    if (current.value if current else None) == value:
        return False
    if value is None:
        session.delete(current)
    elif current is None:
        session.add(Setting(key=key, value=value, updated_at=now))
    else:
        current.value = value
        current.updated_at = now
    return True


def update(
    session: Session,
    now: datetime,
    *,
    name: str | None = None,
    name_highlight: str | None = None,
    css: str | None = None,
    icon: bytes | None = None,
    clear_icon: bool = False,
    reset: bool = False,
) -> list[str]:
    """Changes what is given, all of it or nothing, and returns which fields really changed. Raises
    InvalidAppearance before writing anything."""
    given = (name, name_highlight, css, icon)
    if reset and (clear_icon or any(value is not None for value in given)):
        raise InvalidAppearance("Reset cannot be combined with other changes.")
    if icon is not None and clear_icon:
        raise InvalidAppearance("Send a new icon or remove the current one, not both.")
    values: dict[str, Any] = dict.fromkeys(_KEYS) if reset else {}
    if clear_icon:
        values["icon"] = None
    if name is not None or name_highlight is not None:
        current = name_of(session)
        final_name = current.text if name is None else _clean_name(name)
        highlight = current.highlight if name_highlight is None else name_highlight
        if highlight not in final_name:
            raise InvalidAppearance(
                "The highlight must be part of the name, exactly as it is written there."
            )
        if name is not None:
            values["name"] = None if final_name == DEFAULT_NAME else final_name
        if name_highlight is not None:
            values["name_highlight"] = name_highlight or None
    if css is not None:
        values["css"] = _clean_css(css)
    if icon is not None:
        values["icon"] = _clean_icon(icon)
    return [field for field, value in values.items() if _put(session, _KEYS[field], value, now)]


def update_by_operator(session: Session, now: datetime, **changes: Any) -> list[str]:
    """`update`, audited, for what has no API token behind it: the web interface and the command
    line. The API's calls are audited by its middleware."""
    changed = update(session, now, **changes)
    if changed:
        audit.record(
            session,
            now,
            "appearance.updated",
            actor_type=ActorType.OPERATOR,
            detail={"changed": changed},
        )
    return changed


def name_of(session: Session) -> Name:
    name = session.get(Setting, _KEYS["name"])
    highlight = session.get(Setting, _KEYS["name_highlight"])
    return Name(name.value if name else DEFAULT_NAME, highlight.value if highlight else "")


def css_of(session: Session) -> str:
    setting = session.get(Setting, _KEYS["css"])
    return setting.value if setting else ""


def icon_of(session: Session) -> Icon | None:
    setting = session.get(Setting, _KEYS["icon"])
    if setting is None:
        return None
    return Icon(setting.value["type"], base64.b64decode(setting.value["base64"]))
