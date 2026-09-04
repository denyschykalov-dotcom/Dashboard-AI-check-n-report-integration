"""Reporting-period windows and cross-month aggregation.

The sheet-backed data sources (GA4, GSC) key every row by a monthly ``Period``
label ("Jun 2026"). By default a report covers a single month — the latest one
present — compared month-over-month and year-over-year. This module generalizes
that to a *window* of consecutive months so a specialist can run a custom date
range or a full-year report:

* ``current`` — the months in the requested range,
* ``previous`` — the equally long span immediately before it,
* ``yoy`` — the same range shifted back twelve months.

With no explicit selection the windows collapse to single months and behave
exactly like the original latest-month logic, so existing single-month reports
are unaffected. When a window spans more than one month the resolvers aggregate
across it (see the ``sum_*`` / ``weighted_avg`` helpers): additive metrics are
summed, rates are recomputed from their components or session-weighted, and
snapshot metrics (position buckets) use the window's most recent month.
"""

from __future__ import annotations

import re
import typing

from dataclasses import dataclass
from datetime import date, datetime


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(value: date) -> str:
    """Canonical ``"Jun 2026"`` label, matching the sheets' ``Period`` column."""
    return f"{_MONTH_ABBR[value.month - 1]} {value.year}"


def parse_label(label: typing.Optional[str]) -> typing.Optional[date]:
    """Parse a ``"Jun 2026"`` label to the first of that month (or None).

    Full month names are accepted too: tab names are typed by hand and some
    collectors write "June 2026". A label that does not parse drops every row of
    that tab silently, which showed up as an empty section (seen on tarsco's
    "GA4 Key Events" tab).
    """
    text = (label or "").strip()
    for fmt in ("%b %Y", "%B %Y"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1)
        except (ValueError, AttributeError):
            continue
    return None


def _shift(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _month_diff(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _months_between(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    out: list[date] = []
    cursor = start.replace(day=1)
    end = end.replace(day=1)
    while cursor <= end:
        out.append(cursor)
        cursor = _shift(cursor, 1)
    return out


@dataclass
class PeriodSelection:
    """A user-chosen reporting range, rounded to whole months."""

    start: date  # first day of the first month in range
    end: date  # first day of the last month in range (inclusive)
    report_type: str = "monthly"  # "monthly" | "yearly" (display hint only)


def parse_selection(
    date_from: typing.Optional[str],
    date_to: typing.Optional[str],
    report_type: typing.Optional[str] = None,
) -> typing.Optional[PeriodSelection]:
    """Build a :class:`PeriodSelection` from ISO date strings (YYYY-MM-DD or
    YYYY-MM). Returns None when either bound is missing/unparseable so callers
    fall back to the default latest-month behaviour.
    """

    start = _parse_iso_month(date_from)
    end = _parse_iso_month(date_to)
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    kind = (report_type or "monthly").strip().lower()
    if kind not in {"monthly", "yearly"}:
        kind = "monthly"
    return PeriodSelection(start=start, end=end, report_type=kind)


# The reporting period a specialist picks in the report builder, as a window
# length in whole months ending with the last *completed* month (reports are
# generated after a period closes).
_PERIOD_PRESETS: dict[str, int] = {
    "last_month": 1,
    "last_3_months": 3,
}

# The comparisons a report can offer. Several may be chosen at once; each one
# becomes a toggle in the exported report:
#   * mom — the current window vs the equally long window immediately before it
#   * yoy — the current window vs the same window twelve months earlier
COMPARISON_MODES: tuple[str, ...] = ("mom", "yoy")

# Legacy single-choice comparison presets. Kept so selections saved before the
# period/comparison split (and older API clients) still resolve; each maps onto
# a period preset plus the comparisons it implied.
_LEGACY_COMPARISON_PRESETS: dict[str, tuple[str, list[str]]] = {
    "last_month_vs_prev": ("last_month", ["mom"]),
    "last_month_vs_year": ("last_month", ["yoy"]),
    "last_3_months_vs_year": ("last_3_months", ["yoy"]),
}


def period_presets() -> list[str]:
    return list(_PERIOD_PRESETS)


def comparison_presets() -> list[str]:
    """The legacy single-choice preset keys (see ``parse_comparison``)."""
    return list(_LEGACY_COMPARISON_PRESETS)


def normalize_comparisons(values: typing.Optional[typing.Iterable[str]]) -> list[str]:
    """Clean a chosen comparison list: known modes only, de-duplicated, order
    preserved (the first is the one the report opens on). Falls back to ``mom``
    so a report always has at least one comparison to show."""
    out: list[str] = []
    for value in values or ():
        mode = str(value or "").strip().lower()
        if mode in COMPARISON_MODES and mode not in out:
            out.append(mode)
    return out or ["mom"]


def parse_period_preset(
    preset: typing.Optional[str],
    now: datetime,
) -> typing.Optional[PeriodSelection]:
    """Resolve a period-preset key ("last_month" / "last_3_months") into a
    concrete month window ending with the last completed month before ``now``.
    Returns None for an unknown/empty preset so callers fall back to the explicit
    custom-range / full-year selection.
    """
    months = _PERIOD_PRESETS.get((preset or "").strip())
    if months is None:
        return None
    last_completed = _shift(date(now.year, now.month, 1), -1)
    return PeriodSelection(
        start=_shift(last_completed, -(months - 1)),
        end=last_completed,
        report_type="monthly",
    )


def legacy_comparison_preset(
    preset: typing.Optional[str],
) -> typing.Optional[tuple[str, list[str]]]:
    """Map a legacy comparison-preset key onto ``(period_preset, comparisons)``."""
    spec = _LEGACY_COMPARISON_PRESETS.get((preset or "").strip())
    if spec is None:
        return None
    period, comparisons = spec
    return period, list(comparisons)


def parse_comparison(
    preset: typing.Optional[str],
    now: datetime,
) -> typing.Optional[tuple[PeriodSelection, str]]:
    """Legacy resolver: a single comparison-preset key → its month window plus
    the comparison mode. Superseded by :func:`parse_period_preset` +
    :func:`normalize_comparisons`, kept for older callers/stored selections.
    """
    spec = legacy_comparison_preset(preset)
    if spec is None:
        return None
    period, comparisons = spec
    selection = parse_period_preset(period, now)
    if selection is None:
        return None
    return selection, comparisons[0]


def _parse_iso_month(value: typing.Optional[str]) -> typing.Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y/%m"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


@dataclass
class Window:
    labels: list[str]  # sheet ``Period`` labels present within this window
    display: typing.Optional[str]  # human label, e.g. "Jan 2026 – Jun 2026"

    @property
    def latest(self) -> typing.Optional[str]:
        """The most recent label in the window — for snapshot-style metrics."""
        best: typing.Optional[str] = None
        best_date: typing.Optional[date] = None
        for label in self.labels:
            parsed = parse_label(label)
            if parsed is not None and (best_date is None or parsed > best_date):
                best_date, best = parsed, label
        return best


@dataclass
class Windows:
    current: Window
    previous: Window
    yoy: Window


def _format_range(months: list[date], report_type: str) -> typing.Optional[str]:
    if not months:
        return None
    first, last = months[0], months[-1]
    if report_type == "yearly" and first.month == 1 and last.month == 12 and first.year == last.year:
        return str(first.year)
    if first == last:
        return month_label(first)
    return f"{month_label(first)} – {month_label(last)}"


def selection_display(selection: PeriodSelection) -> str:
    """The human label for a selection's whole range (e.g. "2026" or
    "Jan 2026 – Jun 2026")."""
    return _format_range(_months_between(selection.start, selection.end), selection.report_type) or ""


def resolve_windows(
    period_labels: typing.Iterable[str],
    selection: typing.Optional[PeriodSelection] = None,
) -> Windows:
    """Resolve current/previous/yoy windows from the labels a sheet actually has.

    Without a selection this mirrors :func:`sheets_client.resolve_periods` — a
    single latest month with month-over-month and year-over-year neighbours.
    """

    by_date: dict[date, str] = {}
    for label in period_labels:
        parsed = parse_label(label)
        if parsed is not None:
            by_date[parsed] = label.strip()

    if selection is None:
        if not by_date:
            empty = Window([], None)
            return Windows(empty, Window([], None), Window([], None))
        current_date = max(by_date)
        return Windows(
            _single_window(by_date, current_date),
            _single_window(by_date, _shift(current_date, -1)),
            _single_window(by_date, _shift(current_date, -12)),
        )

    span = _month_diff(selection.start, selection.end) + 1
    current_months = _months_between(selection.start, selection.end)
    previous_end = _shift(selection.start, -1)
    previous_start = _shift(previous_end, -(span - 1))
    previous_months = _months_between(previous_start, previous_end)
    yoy_months = [_shift(month, -12) for month in current_months]

    return Windows(
        _range_window(by_date, current_months, selection.report_type),
        _range_window(by_date, previous_months, selection.report_type),
        _range_window(by_date, yoy_months, selection.report_type),
    )


def _single_window(by_date: dict[date, str], when: date) -> Window:
    label = by_date.get(when)
    # display mirrors the legacy single-label behaviour: the label, or None when
    # that month isn't present in the sheet.
    return Window([label] if label else [], label)


def _range_window(by_date: dict[date, str], months: list[date], report_type: str) -> Window:
    labels = [by_date[month] for month in months if month in by_date]
    return Window(labels, _format_range(months, report_type))


# --- aggregation helpers -----------------------------------------------------


# Sheets values come back FORMATTED, so a number carries the spreadsheet's
# locale: "12 345,67" (UA/EU), "₴12,345.67", "1 234" with a non-breaking space.
# Everything that is not a digit, separator or sign is dropped before parsing.
_NUM_JUNK = re.compile(r"[^0-9,.\-]")
_THOUSANDS_COMMA = re.compile(r"-?\d{1,3}(,\d{3})+$")


def num(value: typing.Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = _NUM_JUNK.sub("", str(value))
    if not text:
        return 0.0
    if "," in text and "." in text:
        # the separator that comes last is the decimal one
        text = (
            text.replace(",", "")
            if text.rfind(".") > text.rfind(",")
            else text.replace(".", "").replace(",", ".")
        )
    elif "," in text:
        # "1,234" is thousands; "12,5" is a decimal comma
        text = text.replace(",", "") if _THOUSANDS_COMMA.match(text) else text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def to_int(value: typing.Any) -> int:
    return int(num(value))


def window_rows(rows: list[dict[str, str]], window: Window) -> list[dict[str, str]]:
    labels = set(window.labels)
    if not labels:
        return []
    return [row for row in rows if (row.get("Period") or "").strip() in labels]


def sum_int(rows: typing.Iterable[dict[str, str]], field: str) -> int:
    return int(sum(num(row.get(field)) for row in rows))


def sum_float(rows: typing.Iterable[dict[str, str]], field: str) -> float:
    return sum(num(row.get(field)) for row in rows)


def weighted_avg(
    rows: typing.Iterable[dict[str, str]], value_field: str, weight_field: str
) -> float:
    total_weight = 0.0
    weighted = 0.0
    for row in rows:
        weight = num(row.get(weight_field))
        weighted += num(row.get(value_field)) * weight
        total_weight += weight
    return round(weighted / total_weight, 1) if total_weight else 0.0


def ratio_pct(numerator: float, denominator: float) -> float:
    return round((numerator / denominator) * 100, 1) if denominator else 0.0


def group_by(rows: typing.Iterable[dict[str, str]], key_field: str) -> dict[str, list[dict[str, str]]]:
    """Group rows by a key column, preserving first-seen key order."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        key = (row.get(key_field) or "").strip()
        grouped.setdefault(key, []).append(row)
    return grouped
