"""Employee Permission (إذن / استئذان) recurrence and overlap logic.

Kept separate from attendance calculations: attendance code only asks
"which approved permission windows exist on this date?".
"""

from datetime import date, datetime, time, timedelta

import frappe
from frappe.utils import get_time, getdate

from mobility_hr.hr_policy.periods import iter_dates, weekday_name

WEEKDAY_FIELDS = {
	"Monday": "on_monday",
	"Tuesday": "on_tuesday",
	"Wednesday": "on_wednesday",
	"Thursday": "on_thursday",
	"Friday": "on_friday",
	"Saturday": "on_saturday",
	"Sunday": "on_sunday",
}
PERMISSION_FIELDS = [
	"name",
	"employee",
	"permission_type",
	"repetition_type",
	"from_date",
	"to_date",
	"start_time",
	"end_time",
	"status",
	"docstatus",
	*WEEKDAY_FIELDS.values(),
]


def effective_to_date(p) -> date:
	if p.get("repetition_type") == "One-time" or not p.get("to_date"):
		return getdate(p.get("from_date"))
	return getdate(p.get("to_date"))


def occurs_on(p, d) -> bool:
	"""Does permission `p` (dict/doc) apply on date `d`?"""
	d = getdate(d)
	start = getdate(p.get("from_date"))
	if d < start or d > effective_to_date(p):
		return False
	rep = p.get("repetition_type") or "One-time"
	if rep in ("One-time", "Daily"):
		return True
	if rep == "Weekly":
		return bool(p.get(WEEKDAY_FIELDS[weekday_name(d)]))
	if rep == "Monthly":
		return d.day == start.day
	return False


def window_on(p, d) -> tuple[datetime, datetime]:
	d = getdate(d)
	return (
		datetime.combine(d, get_time(p.get("start_time"))),
		datetime.combine(d, get_time(p.get("end_time"))),
	)


def duration_minutes(p) -> int:
	start, end = window_on(p, date(2000, 1, 1))
	return max(int((end - start).total_seconds() // 60), 0)


def find_overlap(p, others) -> tuple[str, date] | None:
	"""First (permission name, date) where `p` and any of `others` overlap in time."""
	p_start, p_end = getdate(p.get("from_date")), effective_to_date(p)
	for other in others:
		lo = max(p_start, getdate(other.from_date))
		hi = min(p_end, effective_to_date(other))
		if lo > hi:
			continue
		for d in iter_dates(lo, hi):
			if occurs_on(p, d) and occurs_on(other, d):
				a1, a2 = window_on(p, d)
				b1, b2 = window_on(other, d)
				if a1 < b2 and b1 < a2:
					return other.name, d
	return None


def get_approved_permissions(employee: str, start, end) -> list[frappe._dict]:
	"""Approved & submitted permissions that may occur within [start, end]."""
	start, end = getdate(start), getdate(end)
	rows = frappe.get_all(
		"Employee Permission",
		filters={"employee": employee, "docstatus": 1, "status": "Approved", "from_date": ["<=", end]},
		fields=PERMISSION_FIELDS,
	)
	return [r for r in rows if effective_to_date(r) >= start]


def windows_for_date(permissions, d) -> list[tuple[datetime, datetime, frappe._dict]]:
	return [(*window_on(p, d), p) for p in permissions if occurs_on(p, d)]


def overlap_minutes(intervals_a, intervals_b) -> int:
	"""Total minutes where two lists of (start, end, ...) intervals overlap."""
	total = 0
	for a in intervals_a:
		for b in intervals_b:
			lo, hi = max(a[0], b[0]), min(a[1], b[1])
			if hi > lo:
				total += (hi - lo).total_seconds() / 60
	return int(round(total))


def count_period_usage(employee: str, start, end, exclude: str | None = None) -> tuple[int, int]:
	"""(number of permission occurrences, total minutes) within [start, end] for limits."""
	rows = frappe.get_all(
		"Employee Permission",
		filters={"employee": employee, "docstatus": ["<", 2], "status": ["in", ["Open", "Approved"]], "from_date": ["<=", getdate(end)]},
		fields=PERMISSION_FIELDS,
	)
	count = minutes = 0
	for p in rows:
		if exclude and p.name == exclude:
			continue
		for d in iter_dates(max(getdate(start), getdate(p.from_date)), min(getdate(end), effective_to_date(p))):
			if occurs_on(p, d):
				count += 1
				minutes += duration_minutes(p)
	return count, minutes
