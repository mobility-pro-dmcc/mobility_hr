"""Attendance.validate hook: handbook half-day rules and Shift Type WFH days.

Runs only for *new* attendance before it is submitted (HRMS auto attendance inserts and submits
in one go), so submitted records are never rewritten. Leave-based attendance is left alone.
The reason for any change is written to `policy_note` on the Attendance.
"""

import frappe
from frappe import _

from mobility_hr.hr_policy import half_day, wfh
from mobility_hr.hr_policy.day_facts import get_single_day
from mobility_hr.hr_policy.handbook import get_handbook


def validate(doc, method=None):
	if not doc.is_new() or doc.docstatus != 0 or doc.leave_application:
		return
	notes = []
	note = classify_half_day(doc)
	if note:
		notes.append(note)
	note = wfh.classify_attendance(doc)
	if note:
		notes.append(note)
	if notes:
		doc.policy_note = "\n".join(notes)


def classify_half_day(doc) -> str | None:
	"""Present ↔ Half Day by handbook rules, for attendance created from check-ins."""
	if doc.status not in ("Present", "Half Day") or not doc.in_time:
		return None
	handbook = get_handbook(doc.employee, doc.attendance_date)
	if not handbook or not handbook.enable_half_day_rules:
		return None

	facts = get_single_day(doc.employee, doc.attendance_date)
	if not facts.is_working_day:
		return None
	worked = facts.worked_minutes or int(round((doc.working_hours or 0) * 60))
	early_covered = facts.early_exit_minutes > 0 and facts.early_permission_minutes >= facts.early_exit_minutes
	is_half, reason = half_day.evaluate(
		handbook,
		worked_minutes=worked,
		standard_minutes=facts.standard_minutes,
		last_out=doc.out_time or facts.last_out,
		permission_minutes=facts.permission_minutes_in_shift(),
		early_permission_covers=early_covered,
		attendance_date=doc.attendance_date,
		first_in=doc.in_time or facts.first_in,
		late_permission_covers=facts.raw_late_minutes > 0 and facts.late_permission_minutes >= facts.raw_late_minutes,
	)
	previous = doc.status
	if is_half:
		doc.status = "Half Day"
		doc.half_day_status = "Absent"
		doc.modify_half_day_status = 0
	else:
		doc.status = "Present"
		doc.half_day_status = None
	if doc.status == previous:
		return _("Handbook {0}: {1}").format(handbook.name, reason) if is_half else None
	return _("Handbook {0} changed {1} → {2}: {3}").format(handbook.name, previous, doc.status, reason)
