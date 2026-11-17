"""Half-day identification (handbook rules) — pure decision logic.

Approved permissions in the shift window count as worked time, so a permitted absence never
turns a day into a half day on its own.
"""

from datetime import datetime

from frappe.utils import flt, get_datetime, get_time


def evaluate(
	handbook,
	worked_minutes: int,
	standard_minutes: int,
	last_out,
	permission_minutes: int,
	early_permission_covers: bool,
	attendance_date,
	first_in=None,
	late_permission_covers: bool = False,
) -> tuple[bool, str]:
	"""Return (is_half_day, reason)."""
	effective_hours = (worked_minutes + permission_minutes) / 60
	method = handbook.half_day_method or "Working Hours Below Minimum"
	checks = []

	if method in ("Working Hours Below Minimum", "Any Condition") and flt(handbook.half_day_min_hours):
		if effective_hours < flt(handbook.half_day_min_hours):
			checks.append(f"worked {effective_hours:.2f} h (incl. {permission_minutes} min permission) < {flt(handbook.half_day_min_hours):g} h")

	if method in ("Attendance Percentage Below Minimum", "Any Condition") and flt(handbook.half_day_min_percentage) and standard_minutes:
		pct = effective_hours * 60 / standard_minutes * 100
		if pct < flt(handbook.half_day_min_percentage):
			checks.append(f"attendance {pct:.0f}% < {flt(handbook.half_day_min_percentage):g}% of shift")

	if method in ("Left Before Cut-off Time", "Any Condition") and handbook.half_day_leave_before and last_out:
		cutoff = datetime.combine(get_datetime(attendance_date).date(), get_time(handbook.half_day_leave_before))
		if get_datetime(last_out) < cutoff and not early_permission_covers:
			checks.append(f"left at {get_datetime(last_out):%H:%M}, before {cutoff:%H:%M}")

	if method in ("Arrived After Cut-off Time", "Any Condition") and handbook.get("half_day_arrive_after") and first_in:
		cutoff = datetime.combine(get_datetime(attendance_date).date(), get_time(handbook.half_day_arrive_after))
		if get_datetime(first_in) > cutoff and not late_permission_covers:
			checks.append(f"arrived at {get_datetime(first_in):%H:%M}, after {cutoff:%H:%M}")

	if checks:
		return True, "Half Day: " + "; ".join(checks)
	return False, "Present: half-day conditions not met"
