"""Employee HR dashboard — service layer (no request handling, no authorization).

All numbers come from existing records:
- days, check-ins, intervals, lateness → day_facts (+ handbook grace via lateness)
- statuses → submitted Attendance
- leave days → approved Leave Applications; balances → HRMS get_leave_details
- deductions → Attendance Penalty / Half Day Leave Deduction (submitted = applied, draft = pending)
- overtime → Employee Overtime (submitted = approved, draft = pending)
Values that cannot be derived reliably are returned as null, never guessed.
"""

from collections import defaultdict

import frappe
from frappe.utils import flt, getdate, today

from mobility_hr.hr_policy import lateness
from mobility_hr.hr_policy.day_facts import get_day_facts
from mobility_hr.hr_policy.handbook import get_employee, get_handbook
from mobility_hr.hr_policy.periods import Period, get_period, get_period_for_date, handbook_start_day

ATTENDED = ("Present", "Work From Home")


def resolve_period(employee: str, year: int | None = None, month: int | None = None) -> tuple[Period, object]:
	"""Attendance period (handbook-aware) and the handbook used to define it."""
	ref = getdate(today())
	handbook = get_handbook(employee, ref)
	if year and month:
		probe = get_period(int(year), int(month), handbook_start_day(handbook))
		handbook = get_handbook(employee, probe.end) or handbook
		return get_period(int(year), int(month), handbook_start_day(handbook)), handbook
	return get_period_for_date(ref, handbook_start_day(handbook)), handbook


def _fmt_time(dt):
	return dt.strftime("%H:%M") if dt else None


def build_days(employee: str, period: Period) -> list[dict]:
	emp = get_employee(employee)
	doj = getdate(emp.date_of_joining) if emp.date_of_joining else None
	relieved = getdate(emp.relieving_date) if emp.relieving_date else None
	now = getdate(today())
	# latest open/approved forgotten-punch request per day (so the portal doesn't offer "Request fix" twice)
	requests = {}
	for r in frappe.get_all(
		"Checkin Request",
		filters={"employee": employee, "docstatus": ["<", 2], "status": ["in", ["Open", "Approved"]], "date": ["between", [period.start, period.end]]},
		fields=["date", "status", "docstatus"],
		order_by="creation asc",
	):
		requests[getdate(r.date)] = "Approved" if r.status == "Approved" and r.docstatus == 1 else "Pending Approval"
	rows = []
	for f in get_day_facts(employee, period.start, period.end):
		employed = (not doj or f.date >= doj) and (not relieved or f.date <= relieved)
		handbook = get_handbook(employee, f.date)
		late = lateness.late_day(f, handbook) if handbook else None
		counted_late = late.counted if late else (max(f.raw_late_minutes - f.late_permission_minutes, 0) if f.worked else 0)

		status = f.attendance_status
		if not employed:
			display = "Not Employed"
		elif status:
			display = status
		elif f.holiday_type:
			display = f.holiday_type
		elif f.leave:
			display = "On Leave"
		elif f.date > now:
			display = "Upcoming"
		elif f.checkins:
			display = "Not Processed"
		elif not f.shift:
			display = "No Shift"
		else:
			display = "Not Marked"

		rows.append(
			{
				"date": str(f.date),
				"day": f.weekday,
				"shift": f.shift,
				"shift_start": _fmt_time(f.shift_start),
				"shift_end": _fmt_time(f.shift_end),
				"first_check_in": _fmt_time(f.first_in),
				"last_check_out": _fmt_time(f.last_out),
				"working_hours": round(f.worked_minutes / 60, 2) if f.intervals else None,
				"intervals": len(f.intervals),
				"late_minutes": counted_late,
				"status": display,
				"attendance": f.attendance.name if f.attendance else None,
				"work_mode": "Work From Home" if status == "Work From Home" else ("Office" if status in ("Present", "Half Day") else None),
				"is_wfh_day": f.is_wfh_day,
				"is_half_day": status == "Half Day",
				"holiday": f.holiday_description if f.holiday_type else None,
				"leave_type": (f.leave.leave_type if f.leave else None) or (f.attendance.leave_type if f.attendance else None),
				"missing_check_in": f.missing_check_in,
				"missing_check_out": f.missing_check_out and f.date < now,
				"checkin_request": requests.get(f.date),
				"permissions": [
					{"type": p.permission_type, "from": _fmt_time(s), "to": _fmt_time(e), "name": p.name}
					for s, e, p in f.permissions
				],
				"_employed": employed,
				"_forgiven": lateness.forgiven_minutes(late) if late else 0,
				"_working_day": f.is_working_day and employed,
				"_leave_days": (0.5 if f.leave.half_day else 1.0) if (f.leave and employed and f.is_working_day) else 0.0,
			}
		)
	return rows


def get_deductions(employee: str, period: Period) -> dict:
	penalties = frappe.get_all(
		"Attendance Penalty",
		filters={"employee": employee, "docstatus": ["<", 2], "period_end": ["between", [period.start, period.end]]},
		fields=["name", "docstatus", "frequency", "period_start", "period_end", "total_late_minutes", "rule_name", "penalty_type", "penalty_value", "deduction_unit", "leave_type", "leave_days_deducted", "deduction_status"],
		order_by="period_start asc",
	)
	half_days = frappe.get_all(
		"Half Day Leave Deduction",
		filters={"employee": employee, "docstatus": ["<", 2], "attendance_date": ["between", [period.start, period.end]]},
		fields=["name", "docstatus", "attendance_date", "leave_type", "deduction_days", "deducted_days", "deduction_status"],
		order_by="attendance_date asc",
	)
	totals = defaultdict(float)
	for p in penalties:
		if p.docstatus == 1 and p.penalty_type != "Warning Only":
			unit = p.deduction_unit or p.penalty_type
			totals[unit] += flt(p.leave_days_deducted) if p.penalty_type == "Leave Days" else flt(p.penalty_value)
	for h in half_days:
		if h.docstatus == 1:
			totals["Days"] += flt(h.deducted_days)
	for row in penalties + half_days:
		row["state"] = "Applied" if row.pop("docstatus") == 1 else "Pending Approval"
	return {"totals": {k: round(v, 2) for k, v in totals.items()}, "penalties": penalties, "half_day_deductions": half_days}


def overtime_applies(handbook) -> bool:
	"""Overtime is shown only when the handbook calculates it (working-day overtime or holiday work)."""
	if not handbook:
		return False
	return bool(handbook.enable_overtime) or (handbook.holiday_work_compensation or "None") != "None"


def get_overtime(employee: str, period: Period) -> dict:
	rows = frappe.get_all(
		"Employee Overtime",
		filters={"employee": employee, "docstatus": ["<", 2], "attendance_date": ["between", [period.start, period.end]]},
		fields=["name", "docstatus", "attendance_date", "day_type", "overtime_minutes", "payable_hours", "amount", "compensation", "comp_leave_type", "comp_leave_days"],
		order_by="attendance_date asc",
	)
	approved = sum(r.overtime_minutes for r in rows if r.docstatus == 1)
	pending = sum(r.overtime_minutes for r in rows if r.docstatus == 0)
	for r in rows:
		r["state"] = "Approved" if r.pop("docstatus") == 1 else "Pending Approval"
	return {"approved_hours": round(approved / 60, 2), "pending_hours": round(pending / 60, 2), "records": rows}


def get_leave_balances(employee: str, on_date=None) -> list[dict]:
	"""Same figures as HRMS `get_leave_details`, built from its calculation functions.

	`get_leave_details` itself also runs a permission-checked Leave Type query, which fails for
	employees without Leave Type read access; authorization is already enforced by the API layer.
	"""
	from hrms.hr.doctype.leave_application.leave_application import (
		get_leave_allocation_records,
		get_leave_balance_on,
		get_leaves_for_period,
		get_leaves_pending_approval_for_period,
	)

	on_date = getdate(on_date or today())
	result = []
	for leave_type, allocation in get_leave_allocation_records(employee, on_date).items():
		remaining = get_leave_balance_on(employee, leave_type, on_date, to_date=allocation.to_date, consider_all_leaves_in_the_allocation_period=True)
		taken = get_leaves_for_period(employee, leave_type, allocation.from_date, allocation.to_date) * -1
		pending = get_leaves_pending_approval_for_period(employee, leave_type, allocation.from_date, allocation.to_date)
		expired = flt(allocation.total_leaves_allocated) - (flt(remaining) + flt(taken))
		result.append(
			{
				"leave_type": leave_type,
				"allocated": flt(allocation.total_leaves_allocated, 2),
				"taken": flt(taken, 2),
				"pending_approval": flt(pending, 2),
				"expired": flt(expired, 2) if expired > 0 else 0,
				"remaining": flt(remaining, 2),
			}
		)
	return result


def annual_leave_type(handbook) -> str | None:
	if handbook and handbook.leave_entitlement_rules:
		return handbook.leave_entitlement_rules[0].leave_type
	return None


def late_total(days: list[dict], handbook) -> int:
	"""Counted late minutes for the period, including grace minutes above the handbook's monthly cap."""
	total = sum(d["late_minutes"] for d in days if d["_employed"])
	cap = int(handbook.max_grace_minutes_per_period or 0) if handbook else 0
	if cap > 0:
		total += max(sum(d["_forgiven"] for d in days if d["_employed"]) - cap, 0)
	return total


def get_summary(employee: str, period: Period, handbook, days: list[dict] | None = None) -> dict:
	days = days if days is not None else build_days(employee, period)
	now = getdate(today())
	statuses = defaultdict(int)
	for d in days:
		if d["_employed"] and d["attendance"]:
			statuses[d["status"]] += 1

	deductions = get_deductions(employee, period)
	overtime = get_overtime(employee, period)
	balances = get_leave_balances(employee, min(period.end, now))
	annual = annual_leave_type(handbook)
	annual_balance = next((b["remaining"] for b in balances if b["leave_type"] == annual), None) if annual else None

	return {
		"total_working_days": sum(1 for d in days if d["_working_day"]),
		"working_days_to_date": sum(1 for d in days if d["_working_day"] and getdate(d["date"]) <= now),
		"actual_worked_days": statuses["Present"] + statuses["Work From Home"] + 0.5 * statuses["Half Day"],
		"absent_days": statuses["Absent"],
		"half_days": statuses["Half Day"],
		"work_from_home_days": statuses["Work From Home"],
		"total_late_minutes": late_total(days, handbook),
		"total_deductions": deductions["totals"],
		"overtime_hours": overtime["approved_hours"],
		"overtime_hours_pending": overtime["pending_hours"],
		"approved_leave_days": sum(d["_leave_days"] for d in days),
		"annual_leave_type": annual,
		"remaining_annual_leave": annual_balance,
	}


def public_days(days: list[dict]) -> list[dict]:
	return [{k: v for k, v in d.items() if not k.startswith("_")} for d in days]


def get_leave_policies(employee: str) -> list[dict]:
	handbook = get_handbook(employee, today())
	if not handbook:
		return []
	keys = (
		"leave_type",
		"min_advance_notice_days",
		"max_requests_per_period",
		"request_limit_period",
		"max_days_per_year",
		"max_consecutive_days",
		"min_request_days",
		"max_request_days",
		"min_service_months",
		"allow_during_probation",
		"require_attachment",
	)
	return [{k: r.get(k) for k in keys} for r in handbook.leave_policy_rules if r.enabled]


def get_permissions(employee: str, period: Period, start: int = 0, page_length: int = 20) -> dict:
	filters = {"employee": employee, "docstatus": ["<", 2], "from_date": ["<=", period.end], "to_date": [">=", period.start]}
	rows = frappe.get_all(
		"Employee Permission",
		filters=filters,
		fields=["name", "permission_type", "repetition_type", "from_date", "to_date", "start_time", "end_time", "duration_minutes", "status", "reason"],
		order_by="from_date desc",
		start=start,
		page_length=page_length,
	)
	for r in rows:
		r["start_time"], r["end_time"] = str(r["start_time"])[:5], str(r["end_time"])[:5]
	return {"records": rows, "total": frappe.db.count("Employee Permission", filters), "start": start, "page_length": page_length}
