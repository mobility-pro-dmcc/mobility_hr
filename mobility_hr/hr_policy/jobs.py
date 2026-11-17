"""Scheduled processing. Every step is idempotent and can be re-run safely.

Per employee (row-locked on Employee so two workers never process the same person at once):
1. Leave entitlement allocations (create / top up handbook allocations).
2. Auto WFH attendance for 'Mark Automatically' shifts (last 7 days).
3. Overtime / holiday work records (last 35 days, completed days only).
4. Late penalties (daily; weekly & monthly for completed periods in the window).
5. Half-day leave deductions.
"""

import frappe
from frappe.utils import getdate, today

from mobility_hr.hr_policy import leave_entitlement, records, wfh
from mobility_hr.hr_policy.handbook import clear_request_cache

STEPS = ("leave_allocation", "wfh_attendance", "overtime", "late_penalties", "half_day_deductions")


def employees_in_scope() -> list[str]:
	companies = frappe.get_all("HR Handbook", filters={"is_active": 1}, pluck="company", distinct=True)
	if not companies:
		return []
	return frappe.get_all("Employee", filters={"status": "Active", "company": ["in", companies]}, pluck="name")


def process_employee(employee: str, as_of=None, steps=STEPS) -> dict:
	as_of = getdate(as_of or today())
	frappe.db.get_value("Employee", employee, "name", for_update=True)
	start, end = records.lookback_window(as_of)
	result = {}
	for step in steps:
		frappe.db.savepoint("hr_policy_step")
		try:
			if step == "leave_allocation":
				result[step] = leave_entitlement.sync_employee(employee, as_of)
			elif step == "wfh_attendance":
				result[step] = wfh.mark_auto_wfh_attendance(employee, *wfh.default_window())
			elif step == "overtime":
				result[step] = records.create_overtime(employee, start, end, as_of)
			elif step == "late_penalties":
				result[step] = [
					name
					for spec in records.collect_late_penalties(employee, start, end, as_of)
					if (name := records.create_penalty(spec))
				]
			elif step == "half_day_deductions":
				result[step] = records.create_half_day_deductions(employee, start, end)
		except Exception:
			frappe.db.rollback(save_point="hr_policy_step")
			frappe.log_error(title=f"HR policy {step} failed for {employee}")
			result[step] = "error (see Error Log)"
	return result


def run_daily():
	"""Scheduler entry point (daily_long)."""
	for employee in employees_in_scope():
		clear_request_cache()
		process_employee(employee)
		frappe.db.commit()


@frappe.whitelist()
def run_now(employee: str | None = None):
	"""HR Manager: run the policy engine now (all employees in background, or one synchronously)."""
	frappe.only_for(("HR Manager", "System Manager"))
	if employee:
		return process_employee(employee)
	frappe.enqueue("mobility_hr.hr_policy.jobs.run_daily", queue="long", timeout=3600, job_id="hr_policy_daily", deduplicate=True)
	return "queued"
