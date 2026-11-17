"""Employee self-service requests: leave, permission, forgotten check-in.

All documents are created through their normal controllers, so every HRMS and HR Handbook
validation runs (balance, overlaps, notice, limits, …). Authorization (own employee only) is done
by the API layer; `ignore_permissions` is used only because self-service users may not have desk
permissions on these doctypes.
"""

import frappe
from frappe import _
from frappe.utils import cint, get_time, getdate, today

from mobility_hr.hr_policy import checkin_requests
from mobility_hr.hr_policy.handbook import get_employee, get_handbook

REQUEST_DOCTYPES = {"Leave Application", "Employee Permission", "Checkin Request"}


def create_leave_application(employee, leave_type, from_date, to_date, reason, half_day=0, half_day_date=None) -> dict:
	from hrms.hr.doctype.leave_application.leave_application import get_leave_approver

	emp = get_employee(employee)
	doc = frappe.get_doc(
		{
			"doctype": "Leave Application",
			"employee": employee,
			"company": emp.company,
			"leave_type": leave_type,
			"from_date": getdate(from_date),
			"to_date": getdate(to_date),
			"half_day": cint(half_day),
			"half_day_date": getdate(half_day_date) if cint(half_day) and half_day_date else None,
			"description": reason,
			"posting_date": today(),
			"status": "Open",
			"leave_approver": get_leave_approver(employee),
			"follow_via_email": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "status": doc.status, "days": doc.total_leave_days}


def create_permission(employee, permission_type, date, start_time, end_time, reason) -> dict:
	approver = frappe.db.get_value("Employee", employee, "leave_approver")
	doc = frappe.get_doc(
		{
			"doctype": "Employee Permission",
			"employee": employee,
			"permission_type": permission_type,
			"repetition_type": "One-time",
			"from_date": getdate(date),
			"start_time": get_time(start_time),
			"end_time": get_time(end_time),
			"reason": reason,
			"status": "Open",
			"approver": approver,
		}
	)
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "status": doc.status, "minutes": doc.duration_minutes}


def create_checkin_request(employee, date, time, reason, log_type=None) -> dict:
	return checkin_requests.create(employee, date, time, reason, log_type)


def _state(status, docstatus) -> str:
	if docstatus == 2 or status == "Cancelled":
		return "Cancelled"
	if status in ("Approved", "Rejected"):
		return status if docstatus == 1 or status == "Rejected" else "Pending Approval"
	return "Pending Approval"


def list_requests(employee, limit: int = 30) -> list[dict]:
	"""Recent leave, permission and check-in requests, newest first."""
	rows = []
	for r in frappe.get_all(
		"Leave Application",
		filters={"employee": employee},
		fields=["name", "leave_type", "from_date", "to_date", "total_leave_days", "status", "docstatus", "creation"],
		order_by="creation desc",
		limit=limit,
	):
		rows.append(
			{
				"doctype": "Leave Application",
				"name": r.name,
				"kind": "Leave",
				"title": r.leave_type,
				"when": str(r.from_date) if r.from_date == r.to_date else f"{r.from_date} → {r.to_date}",
				"detail": _("{0} day(s)").format(r.total_leave_days),
				"state": _state(r.status, r.docstatus),
				"can_withdraw": r.docstatus == 0 and r.status == "Open",
				"created": r.creation,
			}
		)
	for r in frappe.get_all(
		"Employee Permission",
		filters={"employee": employee},
		fields=["name", "permission_type", "from_date", "start_time", "end_time", "duration_minutes", "status", "docstatus", "creation"],
		order_by="creation desc",
		limit=limit,
	):
		rows.append(
			{
				"doctype": "Employee Permission",
				"name": r.name,
				"kind": "Permission",
				"title": r.permission_type,
				"when": f"{r.from_date} · {str(r.start_time)[:5]}–{str(r.end_time)[:5]}",
				"detail": _("{0} min").format(r.duration_minutes),
				"state": _state(r.status, r.docstatus),
				"can_withdraw": r.docstatus == 0 and r.status == "Open",
				"created": r.creation,
			}
		)
	for r in frappe.get_all(
		"Checkin Request",
		filters={"employee": employee},
		fields=["name", "date", "time", "log_type", "status", "docstatus", "creation"],
		order_by="creation desc",
		limit=limit,
	):
		rows.append(
			{
				"doctype": "Checkin Request",
				"name": r.name,
				"kind": "Check-in",
				"title": _("Forgotten {0}").format(_("check-out") if r.log_type == "OUT" else _("check-in") if r.log_type == "IN" else _("punch")),
				"when": f"{r.date} · {str(r.time)[:5]}",
				"detail": "",
				"state": _state(r.status, r.docstatus),
				"can_withdraw": r.docstatus == 0 and r.status == "Open",
				"created": r.creation,
			}
		)
	rows.sort(key=lambda r: r["created"], reverse=True)
	for r in rows:
		r["created"] = str(r["created"])
	return rows[:limit]


def withdraw(employee, doctype, name) -> None:
	"""Employee withdraws their own request while it is still an open draft."""
	if doctype not in REQUEST_DOCTYPES:
		frappe.throw(_("Unknown request type."), frappe.ValidationError)
	doc = frappe.get_doc(doctype, name)
	if doc.employee != employee:
		raise frappe.PermissionError(_("This request is not yours."))
	if doc.docstatus != 0 or doc.status != "Open":
		frappe.throw(_("Only open requests can be withdrawn."), frappe.ValidationError)
	frappe.delete_doc(doctype, name, ignore_permissions=True)


def request_options(employee) -> dict:
	"""What the employee can request, with the rules that apply (for forms and hints)."""
	from mobility_hr.hr_policy.dashboard import get_leave_balances

	handbook = get_handbook(employee, today())
	policies = {r.leave_type: r for r in (handbook.leave_policy_rules if handbook else []) if r.enabled}
	leave_types = []
	for b in get_leave_balances(employee):
		rule = policies.get(b["leave_type"])
		hints = []
		if rule:
			if cint(rule.min_advance_notice_days):
				hints.append(_("{0} days' notice").format(rule.min_advance_notice_days))
			if rule.max_request_days:
				hints.append(_("max {0} days per request").format(rule.max_request_days))
			if rule.require_attachment:
				hints.append(_("certificate required"))
			if rule.get("deduct_from_leave_type"):
				hints.append(_("also deducted from {0}").format(rule.deduct_from_leave_type))
		leave_types.append({**b, "hints": hints})
	return {
		"leave_types": leave_types,
		"permission_types": ["Late Arrival", "Early Departure", "Temporary Absence"],
		"permission_limits": {
			"per_period": cint(handbook.max_permissions_per_month) if handbook else 0,
			"minutes_per_period": cint(handbook.max_permission_minutes_per_month) if handbook else 0,
		},
		"checkin_request_limit": cint(handbook.get("max_checkin_requests_per_month")) if handbook else 0,
	}
