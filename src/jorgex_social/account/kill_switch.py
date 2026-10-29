"""The kill switch: while on, nothing is written to the network (no containers, no publishing,
and from step 7 no replies). Reads such as snapshots and connection checks go on."""

from datetime import datetime

from sqlalchemy.orm import Session

from jorgex_social.account.models import Setting

KEY = "kill_switch"


def is_on(session: Session) -> bool:
    setting = session.get(Setting, KEY)
    return bool(setting and setting.value)


def set_state(session: Session, enabled: bool, now: datetime) -> Setting:
    current = session.get(Setting, KEY)
    if current is None:
        current = Setting(key=KEY, value=enabled, updated_at=now)
        session.add(current)
    elif current.value != enabled:
        current.value = enabled
        current.updated_at = now
    return current
