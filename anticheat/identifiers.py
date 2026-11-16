"""Stable IDs do not depend on worker restarts or local filesystem paths."""

from datetime import datetime, timezone
from uuid import UUID, uuid5

NAMESPACE = UUID("df4805ca-d685-4950-aa70-ec9eef5b1b65")


def stable_id(kind, value):
    return uuid5(NAMESPACE, f"{kind}:{value}")


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None
