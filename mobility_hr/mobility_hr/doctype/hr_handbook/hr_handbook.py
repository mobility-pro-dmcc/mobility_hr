# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate

from mobility_hr.hr_policy.lateness import UNITS
from mobility_hr.hr_policy.periods import normalize_start_day

GENERATED_DOCTYPES = ("Attendance Penalty", "Employee Overtime", "Half Day Leave Deduction")


class HRHandbook(Document):
	def validate(self):
		self.validate_dates()
		self.validate_period()
		self.validate_late_rules()
		self.validate_overtime_rules()
		self.validate_entitlement_rules()
		self.validate_unique_rules()
		self.validate_conflicts()

	def on_update(self):
		frappe.clear_document_cache(self.doctype, self.name)
		used = sum(frappe.db.count(dt, {"handbook": self.name}) for dt in GENERATED_DOCTYPES)
		if used and not self.is_new():
			frappe.msgprint(
				_("{0} record(s) already generated from this handbook keep the rules they were calculated with. Changes apply to future calculations only.").format(used),
				indicator="blue",
				alert=True,
			)

	def validate_dates(self):
		if self.effective_to and getdate(self.effective_to) < getdate(self.effective_from):
			frappe.throw(_("Effective To cannot be before Effective From."))
		if not self.applies_to_all_employees and not self.applicability:
			frappe.throw(_("Add at least one Applicability row or tick 'Applies to All Employees'."))

	def validate_period(self):
		if self.attendance_period_type == "Custom Period":
			try:
				start = normalize_start_day(self.period_start_day)
			except ValueError:
				frappe.throw(_("Month Start Day must be between 1 and 28."))
			self.period_end_day = 31 if start == 1 else start - 1
		else:
			self.period_start_day, self.period_end_day = 1, 31

	def validate_late_rules(self):
		if self.late_grace_source == "Handbook Grace Period" and cint(self.late_grace_minutes) < 0:
			frappe.throw(_("Grace minutes cannot be negative."))
		for r in self.late_penalty_rules:
			r.deduction_unit = UNITS.get(r.penalty_type)
			if r.condition == "Between" and cint(r.max_minutes) < cint(r.min_minutes):
				frappe.throw(_("Late Penalty Rules row {0}: Maximum must be ≥ Minimum.").format(r.idx))
			if r.penalty_type != "Warning Only" and not flt(r.penalty_value):
				frappe.throw(_("Late Penalty Rules row {0}: set a Penalty Value.").format(r.idx))
			if r.penalty_type == "Leave Days" and not r.leave_type:
				frappe.throw(_("Late Penalty Rules row {0}: Leave Type is required.").format(r.idx))
		# overlapping ranges of the same frequency make the outcome ambiguous
		for freq in ("Daily", "Weekly", "Monthly"):
			ranges = sorted(
				(cint(r.min_minutes), cint(r.max_minutes) if r.condition == "Between" else 10**9, r.idx)
				for r in self.late_penalty_rules
				if r.enabled and r.frequency == freq
			)
			for a, b in zip(ranges, ranges[1:]):
				if b[0] <= a[1]:
					frappe.msgprint(
						_("{0} late rules in rows {1} and {2} overlap; the rule with the higher minimum applies.").format(freq, a[2], b[2]),
						indicator="orange",
					)

	def validate_overtime_rules(self):
		seen = set()
		for r in self.overtime_rules:
			if not r.enabled:
				continue
			key = (r.day_type, r.time_of_day or "Any")
			if key in seen:
				frappe.throw(_("Overtime Rules row {0}: only one enabled rule per Day Type and Time of Day.").format(r.idx))
			seen.add(key)
			if r.calculation_method == "Fixed Hourly Rate" and not flt(r.fixed_rate):
				frappe.throw(_("Overtime Rules row {0}: set the Fixed Hourly Rate.").format(r.idx))
			if r.calculation_method != "Fixed Hourly Rate" and not flt(r.multiplier):
				frappe.throw(_("Overtime Rules row {0}: set the Multiplier.").format(r.idx))
		if bool(self.night_start) != bool(self.night_end):
			frappe.throw(_("Set both Night Starts At and Night Ends At, or neither."))
		if "Compensatory" in (self.holiday_work_compensation or "") and not self.holiday_comp_leave_type:
			frappe.throw(_("Select the Compensatory Leave Type for holiday work."))

	def validate_entitlement_rules(self):
		for r in self.leave_entitlement_rules:
			if flt(r.max_service_years) and flt(r.max_service_years) <= flt(r.min_service_years):
				frappe.throw(_("Leave Entitlement Rules row {0}: Max must be greater than Min.").format(r.idx))
		by_type = {}
		for r in self.leave_entitlement_rules:
			by_type.setdefault(r.leave_type, []).append(r)
		for leave_type, rows in by_type.items():
			rows = sorted(rows, key=lambda r: flt(r.min_service_years))
			for a, b in zip(rows, rows[1:]):
				if flt(a.min_service_years) == flt(b.min_service_years):
					frappe.throw(_("Leave Entitlement Rules: rows {0} and {1} have the same minimum for {2}.").format(a.idx, b.idx, leave_type))

	def validate_unique_rules(self):
		types = [r.leave_type for r in self.leave_policy_rules if r.enabled]
		dupes = {t for t in types if types.count(t) > 1}
		if dupes:
			frappe.throw(_("Leave Policy Rules: only one enabled rule per Leave Type ({0}).").format(", ".join(dupes)))

	def validate_conflicts(self):
		"""Two company-wide handbooks with equal priority and overlapping dates cannot be resolved."""
		if not (self.is_active and self.applies_to_all_employees):
			return
		others = frappe.get_all(
			"HR Handbook",
			filters={
				"name": ["!=", self.name],
				"company": self.company,
				"is_active": 1,
				"applies_to_all_employees": 1,
				"priority": cint(self.priority),
			},
			fields=["name", "effective_from", "effective_to"],
		)
		for o in others:
			a1, a2 = getdate(self.effective_from), getdate(self.effective_to) if self.effective_to else None
			b1, b2 = getdate(o.effective_from), getdate(o.effective_to) if o.effective_to else None
			if (a2 is None or b1 <= a2) and (b2 is None or a1 <= b2):
				frappe.throw(
					_("Handbook {0} also applies to all employees of {1} with priority {2} for overlapping dates. Change the priority or the effective dates.").format(
						frappe.bold(o.name), self.company, cint(self.priority)
					)
				)
