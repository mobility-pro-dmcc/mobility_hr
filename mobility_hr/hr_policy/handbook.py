"""Resolve which HR Handbook applies to an employee on a date.

Resolution rule (deterministic):
1. Only active handbooks of the employee's company whose effective range covers the date.
2. The handbook must match the employee (applies to all, or any applicability row matches).
3. Highest `priority` wins.
4. Tie → most specific match (Employee > Employee Group > Designation > Employee Grade >
   Department > Branch > Employment Type > all employees).
5. Tie → latest `effective_from`, then name (stable).
"""

import json

import frappe

from mobility_hr.hr_policy.cache import clear, request_cache
from frappe.utils import getdate

SPECIFICITY = {
	"Employee": 7,
	"Employee Group": 6,
	"Designation": 5,
	"Employee Grade": 4,
	"Department": 3,
	"Branch": 2,
	"Employment Type": 1,
}
EMPLOYEE_FIELD = {
	"Employee": "name",
	"Designation": "designation",
	"Employee Grade": "grade",
	"Department": "department",
	"Branch": "branch",
	"Employment Type": "employment_type",
}


def get_employee(employee: str) -> frappe._dict:
	cache = request_cache("hr_policy_employee_cache")
	if employee not in cache:
		cache[employee] = frappe.db.get_value(
			"Employee",
			employee,
			[
				"name",
				"employee_name",
				"company",
				"department",
				"branch",
				"designation",
				"grade",
				"employment_type",
				"date_of_joining",
				"relieving_date",
				"status",
				"holiday_list",
				"default_shift",
				"user_id",
				"final_confirmation_date",
				"scheduled_confirmation_date",
			],
			as_dict=True,
		)
	return cache[employee]


def _employee_groups(employee: str) -> set[str]:
	return set(
		frappe.get_all("Employee Group Table", filters={"employee": employee, "parenttype": "Employee Group"}, pluck="parent")
	)


def match_specificity(handbook, employee: frappe._dict, groups: set[str] | None = None) -> int | None:
	"""Specificity of the match, or None when the handbook does not apply to the employee."""
	if handbook.applies_to_all_employees:
		return 0
	best = None
	for row in handbook.applicability or []:
		if row.applicable_on == "Employee Group":
			if groups is None:
				groups = _employee_groups(employee.name)
			matched = row.value in groups
		else:
			matched = employee.get(EMPLOYEE_FIELD[row.applicable_on]) == row.value
		if matched:
			best = max(best or 0, SPECIFICITY[row.applicable_on])
	return best


def get_handbook_name(employee: str, on_date) -> str | None:
	on_date = getdate(on_date)
	key = (employee, on_date)
	cache = request_cache("hr_policy_handbook_cache")
	if key in cache:
		return cache[key]

	emp = get_employee(employee)
	result = None
	if emp:
		candidates = frappe.get_all(
			"HR Handbook",
			filters={"company": emp.company, "is_active": 1, "effective_from": ["<=", on_date]},
			or_filters=[["effective_to", "is", "not set"], ["effective_to", ">=", on_date]],
			fields=["name", "priority", "effective_from"],
		)
		ranked = []
		groups = None
		for c in candidates:
			doc = frappe.get_cached_doc("HR Handbook", c.name)
			if groups is None and any(r.applicable_on == "Employee Group" for r in doc.applicability or []):
				groups = _employee_groups(emp.name)
			spec = match_specificity(doc, emp, groups)
			if spec is not None:
				ranked.append((c.priority or 0, spec, getdate(c.effective_from), c.name))
		if ranked:
			ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3]), reverse=True)
			result = ranked[0][3]

	cache[key] = result
	return result


def get_handbook(employee: str, on_date):
	name = get_handbook_name(employee, on_date)
	return frappe.get_cached_doc("HR Handbook", name) if name else None


def snapshot(row, extra: dict | None = None) -> str:
	"""JSON snapshot of a rule row (and handbook settings) stored on generated records."""
	data = {k: v for k, v in row.as_dict().items() if k not in ("creation", "modified", "owner", "modified_by", "docstatus", "doctype", "parenttype", "parentfield")} if row else {}
	if extra:
		data.update(extra)
	return json.dumps(data, default=str, indent=1, sort_keys=True)


def clear_request_cache():
	clear("hr_policy_employee_cache", "hr_policy_handbook_cache")
