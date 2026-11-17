"""Service-based leave entitlement and automatic Leave Allocations.

Target for a leave period P = Σ over days d from the eligibility start to the accrual end of
  annual_entitlement(rule applicable on min(d, today)) / days_in_P
- Past days use the rule that applied on that day; future days use today's rule, so when a
  milestone is reached the employee is topped up for the rest of the period only.
- Annual frequency accrues to the period end (allocated upfront); Monthly accrues to the end
  of the current month (1/12 per month).
- Proration "None" ignores the join day (full amount for Annual, whole months for Monthly);
  "by Remaining Months" counts whole months; "by Remaining Days" counts exact days.
- Rounding follows the Leave Type's own "Rounding" setting when set (HRMS earned-leave field).

Allocations created here carry `hr_handbook` + `handbook_auto_allocated`; only those are ever
increased by the job. Manual allocations are never modified, and allocations are never
reduced automatically (history is preserved).
"""

from dataclasses import dataclass
from datetime import date

import frappe
from dateutil.relativedelta import relativedelta
from frappe import _
from frappe.utils import flt, getdate, today

from mobility_hr.hr_policy.handbook import get_employee, get_handbook
from mobility_hr.hr_policy.leave_adjustment import change_allocation


@dataclass
class LeavePeriodRange:
	start: date
	end: date
	name: str | None = None


def completed_years(date_of_joining, on_date) -> int:
	if not date_of_joining:
		return 0
	return max(relativedelta(getdate(on_date), getdate(date_of_joining)).years, 0)


def service_years(date_of_joining, on_date) -> float:
	"""Service in years measured in completed months (6 months = 0.5), for rule matching."""
	if not date_of_joining:
		return 0.0
	delta = relativedelta(getdate(on_date), getdate(date_of_joining))
	return max(delta.years * 12 + delta.months, 0) / 12


def rule_for(rules, leave_type: str, years: float):
	"""Rule with min ≤ years < max (max 0 = open); highest min wins."""
	matches = [
		r
		for r in rules or []
		if r.leave_type == leave_type
		and years >= flt(r.min_service_years)
		and (not flt(r.max_service_years) or years < flt(r.max_service_years))
	]
	return max(matches, key=lambda r: flt(r.min_service_years)) if matches else None


def get_leave_period(company: str, on_date) -> LeavePeriodRange:
	from hrms.hr.utils import get_leave_period as hrms_leave_period

	d = getdate(on_date)
	period = hrms_leave_period(d, d, company)
	if period:
		return LeavePeriodRange(getdate(period[0].from_date), getdate(period[0].to_date), period[0].name)
	return LeavePeriodRange(date(d.year, 1, 1), date(d.year, 12, 31))


def month_end(d: date) -> date:
	return (date(d.year, d.month, 1) + relativedelta(months=1)) - relativedelta(days=1)


def calculate_target(rules, leave_type: str, doj, period: LeavePeriodRange, as_of) -> tuple[float, object]:
	"""(target allocation for the period as of `as_of`, rule applicable on `as_of`)."""
	as_of = getdate(as_of)
	current_rule = rule_for(rules, leave_type, service_years(doj, as_of))
	if not current_rule:
		return 0.0, None

	elig_start = max(period.start, getdate(doj)) if doj else period.start
	if elig_start > period.end:
		return 0.0, current_rule

	frequency = current_rule.allocation_frequency or "Annual"
	proration = current_rule.proration_method or "None"
	accrue_to = period.end if frequency == "Annual" else min(month_end(as_of), period.end)

	if proration == "None" and frequency == "Annual":
		return flt(current_rule.annual_entitlement), current_rule

	def annual_on(d):
		rule = rule_for(rules, leave_type, service_years(doj, min(d, as_of)))
		return flt(rule.annual_entitlement) if rule else 0.0

	target = 0.0
	if proration == "Prorate by Remaining Days":
		total_days = (period.end - period.start).days + 1
		d = elig_start
		while d <= accrue_to:
			target += annual_on(d) / total_days
			d += relativedelta(days=1)
		return target, current_rule

	# Month-based: each month of the period is worth 1/n of the annual entitlement.
	months_in_period = max(
		(period.end.year - period.start.year) * 12 + period.end.month - period.start.month + 1, 1
	)
	first_month = date(elig_start.year, elig_start.month, 1)
	if proration == "Prorate by Remaining Months" and elig_start.day != 1 and elig_start != period.start:
		first_month += relativedelta(months=1)  # partial joining month is not counted
	m = max(first_month, date(period.start.year, period.start.month, 1))
	while m <= accrue_to:
		target += annual_on(max(m, elig_start)) / months_in_period
		m += relativedelta(months=1)
	return target, current_rule


def apply_rounding(leave_type: str, value: float) -> float:
	rounding = frappe.get_cached_value("Leave Type", leave_type, "rounding")
	if rounding in ("0.25", "0.5", "1.0", "1"):
		step = flt(rounding)
		return round(value / step) * step
	return round(value, 2)


def find_allocations(employee: str, leave_type: str, period: LeavePeriodRange) -> list[frappe._dict]:
	return frappe.get_all(
		"Leave Allocation",
		filters={
			"employee": employee,
			"leave_type": leave_type,
			"docstatus": 1,
			"from_date": ["<=", period.end],
			"to_date": [">=", period.start],
		},
		fields=["name", "new_leaves_allocated", "handbook_auto_allocated", "hr_handbook", "from_date", "to_date"],
	)


def sync_employee(employee: str, as_of=None) -> list[dict]:
	"""Create / top up handbook allocations for the leave period containing `as_of`. Idempotent."""
	as_of = getdate(as_of or today())
	emp = get_employee(employee)
	if not emp or emp.status != "Active" or not emp.date_of_joining:
		return []
	handbook = get_handbook(employee, as_of)
	if not handbook or not handbook.enable_auto_leave_allocation:
		return []

	period = get_leave_period(emp.company, as_of)
	results = []
	for leave_type in sorted({r.leave_type for r in handbook.leave_entitlement_rules}):
		target, rule = calculate_target(handbook.leave_entitlement_rules, leave_type, emp.date_of_joining, period, as_of)
		target = apply_rounding(leave_type, target)
		existing = find_allocations(employee, leave_type, period)
		manual = [a for a in existing if not a.handbook_auto_allocated]
		ours = [a for a in existing if a.handbook_auto_allocated]
		action = {"leave_type": leave_type, "target": target, "period": f"{period.start}…{period.end}"}

		if manual:
			action["result"] = f"skipped: manual allocation {manual[0].name} exists"
		elif target <= 0:
			action["result"] = "not eligible yet"
		elif not ours:
			action["allocation"] = create_allocation(emp, handbook, rule, leave_type, period, as_of, target)
			action["result"] = "created"
		else:
			current = flt(ours[0].new_leaves_allocated)
			if target > current + 0.001:
				reason = _("Handbook {0}: entitlement now {1} day(s) for {2} completed service year(s).").format(
					handbook.name, target, completed_years(emp.date_of_joining, as_of)
				)
				change_allocation(ours[0].name, target - current, "HR Handbook", handbook.name, reason)
				action.update(allocation=ours[0].name, result=f"increased {current} → {target}")
			else:
				action.update(allocation=ours[0].name, result="up to date")
		results.append(action)
	return results


def create_allocation(emp, handbook, rule, leave_type, period: LeavePeriodRange, as_of, target) -> str:
	from_date = max(period.start, getdate(emp.date_of_joining))
	carry_forward = 0
	policy = next((r for r in handbook.leave_policy_rules if r.enabled and r.leave_type == leave_type), None)
	if (rule and rule.carry_forward) or (policy and policy.allow_carry_forward):
		carry_forward = 1 if frappe.get_cached_value("Leave Type", leave_type, "is_carry_forward") else 0

	doc = frappe.get_doc(
		{
			"doctype": "Leave Allocation",
			"employee": emp.name,
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": period.end,
			"leave_period": period.name,
			"new_leaves_allocated": target,
			"carry_forward": carry_forward,
			"hr_handbook": handbook.name,
			"handbook_auto_allocated": 1,
			"description": _("Automatic allocation from HR Handbook {0}: {1} completed service year(s), rule {2}+ years → {3} day(s)/year ({4}, {5}).").format(
				handbook.name,
				completed_years(emp.date_of_joining, as_of),
				flt(rule.min_service_years) if rule else 0,
				flt(rule.annual_entitlement) if rule else 0,
				rule.allocation_frequency if rule else "",
				rule.proration_method if rule else "",
			),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return doc.name


def cap_carry_forward(doc, method=None):
	"""Leave Allocation.validate: apply the handbook's 'Max Carry Forward Days'."""
	if not (doc.get("handbook_auto_allocated") and doc.get("hr_handbook") and doc.carry_forward and doc.unused_leaves):
		return
	handbook = frappe.get_cached_doc("HR Handbook", doc.hr_handbook)
	policy = next((r for r in handbook.leave_policy_rules if r.enabled and r.leave_type == doc.leave_type), None)
	if policy and flt(policy.max_carry_forward_days) and flt(doc.unused_leaves) > flt(policy.max_carry_forward_days):
		doc.unused_leaves = flt(policy.max_carry_forward_days)
		doc.total_leaves_allocated = flt(doc.unused_leaves) + flt(doc.new_leaves_allocated)
