from collections import Counter
import logging

from .archives import records
from .models import MovementKind
from .viewer import archive_path

LOG = logging.getLogger(__name__)
EVENT_KINDS = {"bhop": MovementKind.BHOP, "gstrafe": MovementKind.GSTRAFE}
BHOP_JUMPBUG = 4
FOG_VALUES = {
    MovementKind.BHOP: range(6),
    MovementKind.GSTRAFE: range(1, 6),
}


def ratio(value, total, scale=1):
    return value * scale / total if value is not None and total else None


def read_events(segment, kind):
    counts = Counter()
    if not hasattr(segment, "archive") or not (segment.journal.integrity or {}).get("complete"):
        return counts
    try:
        with archive_path(segment.archive) as path:
            for row in records(path):
                event = row["data"]
                if EVENT_KINDS.get(event["t"]) != kind or event.get("s") != segment.segment_number:
                    continue
                fog, ideal = event.get("fog"), event.get("ideal")
                if type(fog) is not int or fog not in FOG_VALUES[kind] or ideal not in (0, 1):
                    continue
                result = "ideal" if ideal else "fail"
                if kind == MovementKind.BHOP and event.get("type") == BHOP_JUMPBUG:
                    result = "jumpbug"
                counts[fog, result] += 1
    except Exception:
        LOG.warning("Could not read session statistics archive for %s", segment.pk, exc_info=True)
        return Counter()
    return counts


def summary_rows(kind, counters, events):
    is_bhop = kind == MovementKind.BHOP
    recorded_total = counters.get("bhops" if is_bhop else "count")
    event_total = sum(events.values())
    use_events = event_total > 0 and (not recorded_total or recorded_total == event_total)
    total = event_total if use_events else recorded_total
    if total is None:
        return []
    if use_events and recorded_total != total:
        counters = {}

    def row(label, count, ideal, streak=None, extra=()):
        return {
            "label": label, "count": count, "overall": ratio(count, total, 100),
            "ideal": ratio(ideal, count, 100), "streak": streak,
            "extra": extra if extra else (None, None, None) if is_bhop else (),
        }

    outcomes = Counter()
    fog_counts = Counter()
    ideal_by_fog = Counter()
    for (fog, result), count in events.items():
        outcomes[result] += count
        fog_counts[fog] += count
        if result == "ideal":
            ideal_by_fog[fog] += count

    ideal = outcomes["ideal"] if use_events else counters.get("ideal") if is_bhop else None
    extra = ()
    if is_bhop:
        extra = (
            ratio(counters.get("ideal_distrib"), total, 100),
            ratio(counters.get("jumpcmd_all"), total),
            ratio(counters.get("fast_scroll"), total, 100),
        )
    rows = [row("Total", total, ideal, extra=extra)]
    rows.append(row("Ideal", ideal, ideal, counters.get("ideal_streak_max") if is_bhop else None))
    fails = outcomes["fail"] if use_events else None
    rows.append(row("Fail", fails, 0, counters.get("fail_streak_max") if is_bhop else None))
    if use_events and outcomes["jumpbug"]:
        rows.append(row("Jumpbug", outcomes["jumpbug"], None))
    for fog in FOG_VALUES[kind]:
        count = fog_counts[fog] if use_events else counters.get(f"fog_{fog}")
        if fog == 0 and not count:
            continue
        fog_ideal = ideal_by_fog[fog] if use_events else counters.get(f"ideal_fog_{fog}") if is_bhop else None
        rows.append(row(f"FOG{fog}", count, fog_ideal, counters.get(f"fog_streak_max_{fog}")))
    return rows


def session_statistics(segment, kind):
    counters = segment.summaries.filter(kind=kind).values_list("counters", flat=True).first() or {}
    return {
        "kind": kind.label,
        "is_bhop": kind == MovementKind.BHOP,
        "rows": summary_rows(kind, counters, read_events(segment, kind)),
    }
