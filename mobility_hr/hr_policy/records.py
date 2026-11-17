"""Create traceable policy records (penalties, overtime, half-day deductions) from day facts.

Idempotency: every record has a deterministic key. If *any* record with that key exists
(draft, submitted or cancelled) nothing is created again — a cancelled record means HR
dismissed it; corrections are made by amending. The caller holds a row lock on the
Employee so two workers cannot create the same record concurrently.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from mobility_hr.hr_policy import half_day, lateness, overtime
from mobility_hr.hr_policy.day_facts import get_day_facts
from mobility_hr.hr_policy.handbook import get_handbook, snapshot
from mobility_hr.hr_policy.periods import get_period_for_date, get_week, handbook_start_day


def _exists(doctype: str, keyfield: str, key: str) -> bool:
	return bool(frappe.db.exists(doctype, {keyfield: key}))


def _finish(doc, requires_approval: bool):
	"""Insert; auto-submit when no approval is required. If the automatic submit fails
	(e.g. insufficient leave balance) the record stays as a draft with the reason noted."""
	doc.flags.ignore_permissions = True
	doc.insert()
	if not requires_approval:
		frappe.db.savepoint("hr_policy_autosubmit")
		try:
			doc.submit()
		except frappe.ValidationError as e:
			frappe.db.rollback(save_point="hr_policy_autosubmit")
			frappe.clear_last_message()
			doc.reload()
			doc.add_comment("Info", _("Not submitted automatically: {0}").format(frappe.utils.strip_html(str(e))))
	return doc.name


# --------------------------------------------------------------------------- late penalties


def collect_late_penalties(employee: str, start, end, as_of) -> list[lateness.PenaltySpec]:
	"""Penalty specs for days in [start, end]; weekly/monthly only for periods completed before `as_of`."""
	start, end, as_of = getdate(start), getdate(end), getdate(as_of)
	# widen to whole weeks / periods so totals are complete
	facts = get_day_facts(employee, start - timedelta(days=40), end)
	specs: list[lateness.PenaltySpec] = []
	weekly: dict = {}
	monthly: dict = {}

	for f in facts:
		handbook = get_handbook(employee, f.date)
		if not handbook or not handbook.enable_late_penalties:
			continue
		day = lateness.late_day(f, handbook)
		if not day:
			continue

		if start <= f.date <= end and f.date < as_of:
			rule = lateness.match_rule(handbook.late_penalty_rules, "Daily", day.counted)
			if rule:
				specs.append(lateness.PenaltySpec(employee, handbook.name, "Daily", f.date, f.date, day.counted, rule, [day]))

		ws, we = get_week(f.date, handbook.week_start_day)
		weekly.setdefault((handbook.name, ws, we), []).append(day)
		p = get_period_for_date(f.date, handbook_start_day(handbook))
		monthly.setdefault((handbook.name, p.start, p.end), []).append(day)

	for frequency, buckets in (("Weekly", weekly), ("Monthly", monthly)):
		for (hb_name, ps, pe), days in buckets.items():
			if pe >= as_of or pe < start:  # only completed periods that end inside the window
				continue
			handbook = frappe.get_cached_doc("HR Handbook", hb_name)
			cap = cint(handbook.max_grace_minutes_per_period) if frequency == "Monthly" else 0
			total, over_cap = lateness.period_total(days, cap)
			rule = lateness.match_rule(handbook.late_penalty_rules, frequency, total)
			if rule:
				spec = lateness.PenaltySpec(employee, hb_name, frequency, ps, pe, total, rule, [d for d in days if d.counted or d.raw])
				spec.grace_over_cap = over_cap
				specs.append(spec)
	return specs


def create_penalty(spec: lateness.PenaltySpec) -> str | None:
	if _exists("Attendance Penalty", "penalty_key", spec.key):
		return None
	handbook = frappe.get_cached_doc("HR Handbook", spec.handbook)
	rule = spec.rule
	doc = frappe.new_doc("Attendance Penalty")
	doc.update(
		{
			"employee": spec.employee,
			"handbook": spec.handbook,
			"frequency": spec.frequency,
			"period_start": spec.period_start,
			"period_end": spec.period_end,
			"total_late_minutes": spec.total_minutes,
			"rule_name": rule.rule_name,
			"penalty_type": rule.penalty_type,
			"penalty_value": flt(rule.penalty_value),
			"deduction_unit": lateness.UNITS.get(rule.penalty_type),
			"leave_type": rule.leave_type if rule.penalty_type == "Leave Days" else None,
			"penalty_key": spec.key,
			"explanation": _("{0} late minute(s) counted {1} ({2} → {3}). Matched rule: {4}. Grace: {5}.").format(
				spec.total_minutes,
				_("on {0}").format(spec.period_start) if spec.frequency == "Daily" else _("in the {0} period").format(spec.frequency.lower()),
				spec.period_start,
				spec.period_end,
				lateness.describe_rule(rule),
				handbook.grace_handling or "",
			)
			+ (
				" "
				+ _("Includes {0} grace minute(s) above the {1}-minute monthly grace allowance.").format(
					spec.grace_over_cap, cint(handbook.max_grace_minutes_per_period)
				)
				if getattr(spec, "grace_over_cap", 0)
				else ""
			),
			"rule_snapshot": snapshot(
				rule,
				{
					"late_grace_source": handbook.late_grace_source,
					"late_grace_minutes": handbook.late_grace_minutes,
					"max_grace_minutes_per_period": handbook.max_grace_minutes_per_period,
					"grace_handling": handbook.grace_handling,
					"week_start_day": handbook.week_start_day,
					"period_start_day": handbook_start_day(handbook),
				},
			),
		}
	)
	for d in spec.days:
		doc.append(
			"details",
			{
				"attendance_date": d.date,
				"attendance": d.attendance,
				"shift": d.shift,
				"shift_start": d.shift_start,
				"first_in": d.first_in,
				"raw_late_minutes": d.raw,
				"permission_minutes": d.permission,
				"grace_minutes": d.grace,
				"counted_late_minutes": d.counted,
			},
		)
	return _finish(doc, bool(handbook.late_penalty_requires_approval))


# --------------------------------------------------------------------------- overtime


def create_overtime(employee: str, start, end, as_of) -> list[str]:
	created = []
	for f in get_day_facts(employee, start, end):
		if f.date >= getdate(as_of) or not f.intervals:
			continue
		if not f.holiday_type and not f.worked:
			continue  # working day without processed attendance yet
		handbook = get_handbook(employee, f.date)
		if not handbook:
			continue
		spec = overtime.calculate(f, handbook)
		if not spec or _exists("Employee Overtime", "overtime_key", spec.key):
			continue
		created.append(_make_overtime_doc(spec, handbook))
	return created


def _make_overtime_doc(spec: overtime.OvertimeSpec, handbook) -> str:
	f = spec.facts
	doc = frappe.new_doc("Employee Overtime")
	explanation = [
		_("{0}: worked {1:.2f} h in {2} interval(s); shift {3} = {4:.2f} h.").format(
			spec.day_type, f.worked_minutes / 60, len(f.intervals), f.shift or "-", f.standard_minutes / 60
		)
	]
	if spec.day_type == "Working Day":
		explanation.append(_("Basis: {0}.").format(handbook.overtime_basis))
	explanation += spec.notes
	if spec.comp_leave_days:
		explanation.append(_("Holiday work earns {0} compensatory day(s) of {1}.").format(spec.comp_leave_days, handbook.holiday_comp_leave_type))

	doc.update(
		{
			"employee": spec.employee,
			"handbook": handbook.name,
			"attendance_date": spec.date,
			"day_type": spec.day_type,
			"attendance": f.attendance.name if f.attendance else None,
			"shift": f.shift,
			"standard_hours": round(f.standard_minutes / 60, 2) if spec.day_type == "Working Day" else 0,
			"worked_hours": round(f.worked_minutes / 60, 2),
			"overtime_minutes": spec.counted_minutes,
			"payable_hours": spec.payable_hours,
			"amount": spec.amount,
			"compensation": spec.compensation,
			"comp_leave_type": handbook.holiday_comp_leave_type if spec.comp_leave_days else None,
			"comp_leave_days": spec.comp_leave_days,
			"overtime_key": spec.key,
			"explanation": "\n".join(explanation),
			"rule_snapshot": frappe.as_json(
				{
					"rules": [frappe.parse_json(snapshot(line.rule)) for line in spec.lines],
					"overtime_basis": handbook.overtime_basis,
					"night": [str(handbook.night_start or ""), str(handbook.night_end or "")],
					"holiday_work_compensation": handbook.holiday_work_compensation,
					"comp_days_weekly_off": handbook.comp_days_weekly_off,
					"comp_days_public_holiday": handbook.comp_days_public_holiday,
				}
			),
		}
	)
	for line in spec.lines:
		doc.append(
			"details",
			{
				"overtime_category": line.rule.overtime_category,
				"time_of_day": line.time_of_day,
				"from_time": line.intervals[0][0] if line.intervals else None,
				"to_time": line.intervals[-1][1] if line.intervals else None,
				"raw_minutes": line.raw_minutes,
				"counted_minutes": line.counted_minutes,
				"multiplier": flt(line.rule.multiplier),
				"fixed_rate": flt(line.rule.fixed_rate),
				"payable_hours": line.payable_hours,
				"amount": line.amount,
				"overtime_type": line.rule.overtime_type,
				"is_statutory": cint(line.rule.is_statutory),
			},
		)
	needs_approval = bool(
		(spec.lines and handbook.overtime_requires_approval)
		or (spec.comp_leave_days and handbook.holiday_work_requires_approval)
	)
	return _finish(doc, needs_approval)


# --------------------------------------------------------------------------- half-day deductions


def create_half_day_deductions(employee: str, start, end) -> list[str]:
	created = []
	for f in get_day_facts(employee, start, end):
		if f.attendance_status != "Half Day" or f.attendance.leave_application:
			continue  # half-day *leave* already consumed leave through HRMS
		handbook = get_handbook(employee, f.date)
		if not handbook or not handbook.enable_half_day_deduction or not handbook.half_day_deduction_leave_type:
			continue
		if handbook.half_day_deduction_trigger == "Half Day Attendance Without Approved Permission" and f.permissions:
			continue
		key = f"{employee}|{f.date}"
		if _exists("Half Day Leave Deduction", "deduction_key", key):
			continue
		doc = frappe.new_doc("Half Day Leave Deduction")
		doc.update(
			{
				"employee": employee,
				"handbook": handbook.name,
				"attendance": f.attendance.name,
				"attendance_date": f.date,
				"leave_type": handbook.half_day_deduction_leave_type,
				"deduction_days": flt(handbook.half_day_deduction_value) or 0.5,
				"deduction_key": key,
				"deduction_status": "Pending",
				"explanation": _("Attendance {0} on {1} is a Half Day ({2} h worked). Handbook {3} deducts {4} day(s) of {5}.").format(
					f.attendance.name,
					f.date,
					round(f.worked_minutes / 60, 2),
					handbook.name,
					flt(handbook.half_day_deduction_value) or 0.5,
					handbook.half_day_deduction_leave_type,
				),
			}
		)
		needs_approval = bool(handbook.half_day_deduction_requires_approval or handbook.half_day_requires_approval)
		created.append(_finish(doc, needs_approval))
	return created


def lookback_window(as_of, days: int = 35):
	as_of = getdate(as_of)
	return add_days(as_of, -days), add_days(as_of, -1)
