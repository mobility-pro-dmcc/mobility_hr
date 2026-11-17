"""Work From Home days configured per Shift Type.

A date is a WFH day when the shift the employee is *assigned* to on that date (Shift
Assignment first, default shift otherwise) lists the weekday, and the date is not a holiday.

Shift Type "WFH Attendance Rule":
- Require Check-in   → attendance created from check-ins on a WFH day is classified
                        "Work From Home" instead of "Present"; no check-in → stays Absent.
- Mark Automatically → the employee is marked "Work From Home" without check-ins
                        (only when no attendance / leave exists for the date).
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from mobility_hr.hr_policy.day_facts import get_single_day

REQUIRE_CHECKIN = "Require Check-in"
AUTO = "Mark Automatically"


def classify_attendance(doc) -> str | None:
	"""Called from Attendance.validate for new records. Returns a note when status changed."""
	if doc.status not in ("Present", "Absent") or doc.leave_application:
		return None
	facts = get_single_day(doc.employee, doc.attendance_date)
	if not facts.is_wfh_day:
		return None
	if doc.status == "Present":
		doc.status = "Work From Home"
		return _("Work From Home day of shift {0} ({1}); attended with check-ins.").format(facts.shift, facts.weekday)
	if doc.status == "Absent" and facts.wfh_rule == AUTO:
		doc.status = "Work From Home"
		return _("Work From Home day of shift {0} ({1}); marked automatically by the shift's WFH rule.").format(facts.shift, facts.weekday)
	return None


def mark_auto_wfh_attendance(employee: str, start, end) -> list[str]:
	"""Create WFH attendance for 'Mark Automatically' days without any attendance. Idempotent."""
	from mobility_hr.hr_policy.day_facts import get_day_facts

	created = []
	for f in get_day_facts(employee, start, end):
		if not f.is_wfh_day or f.wfh_rule != AUTO or f.attendance or f.leave:
			continue
		if frappe.db.exists("Attendance", {"employee": employee, "attendance_date": f.date, "docstatus": ["<", 2]}):
			continue
		att = frappe.new_doc("Attendance")
		att.update(
			{
				"employee": employee,
				"attendance_date": f.date,
				"status": "Work From Home",
				"shift": f.shift,
				"company": frappe.db.get_value("Employee", employee, "company"),
			}
		)
		att.flags.ignore_permissions = True
		att.insert()
		att.submit()
		created.append(att.name)
	return created


def default_window() -> tuple:
	"""Days the daily job (re)visits: the last 7 completed days."""
	return add_days(today(), -7), add_days(today(), -1)


def is_wfh_day(employee: str, d) -> bool:
	return get_single_day(employee, getdate(d)).is_wfh_day
