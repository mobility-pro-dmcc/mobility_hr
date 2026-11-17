# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_time, getdate, now_datetime

from mobility_hr.hr_policy import permissions as svc
from mobility_hr.hr_policy.handbook import get_handbook
from mobility_hr.hr_policy.periods import get_period_for_date, handbook_start_day, iter_dates

HR_ROLES = ("HR Manager", "HR User", "System Manager")


class EmployeePermission(Document):
	def validate(self):
		if self.repetition_type == "One-time" or not self.to_date:
			self.to_date = self.from_date
		if getdate(self.to_date) < getdate(self.from_date):
			frappe.throw(_("To Date cannot be before From Date."))
		if get_time(self.end_time) <= get_time(self.start_time):
			frappe.throw(_("End Time must be after Start Time (permissions cannot cross midnight)."))
		if self.repetition_type == "Weekly" and not any(self.get(f) for f in svc.WEEKDAY_FIELDS.values()):
			frappe.throw(_("Select at least one weekday for a weekly permission."))
		if not any(svc.occurs_on(self, d) for d in iter_dates(self.from_date, self.to_date)):
			frappe.throw(_("This permission never occurs between From Date and To Date."))

		self.duration_minutes = svc.duration_minutes(self)
		handbook = get_handbook(self.employee, self.from_date)
		self.handbook = handbook.name if handbook else None

		self.validate_status_change()
		if self.status in ("Open", "Approved"):
			self.validate_overlap()
			self.validate_limits(handbook)

	def validate_status_change(self):
		if self.is_new() or not self.has_value_changed("status"):
			return
		if self.status in ("Approved", "Rejected"):
			user = frappe.session.user
			if not (set(HR_ROLES) & set(frappe.get_roles()) or (self.approver and self.approver == user)):
				frappe.throw(_("Only the approver or HR can approve or reject a permission."))
			employee_user = frappe.db.get_value("Employee", self.employee, "user_id")
			if employee_user == user and not set(HR_ROLES) & set(frappe.get_roles()):
				frappe.throw(_("You cannot approve your own permission."))
			self.approved_by = user
			self.approved_on = now_datetime()

	def validate_overlap(self):
		others = frappe.get_all(
			"Employee Permission",
			filters={
				"employee": self.employee,
				"name": ["!=", self.name or ""],
				"docstatus": ["<", 2],
				"status": ["in", ["Open", "Approved"]],
				"from_date": ["<=", self.to_date],
				"to_date": [">=", self.from_date],
			},
			fields=svc.PERMISSION_FIELDS,
		)
		clash = svc.find_overlap(self, others)
		if clash:
			frappe.throw(
				_("This permission overlaps {0} on {1}.").format(frappe.get_desk_link("Employee Permission", clash[0]), clash[1]),
				title=_("Overlapping Permission"),
			)

	def validate_limits(self, handbook):
		if not handbook:
			return
		max_count = cint(handbook.max_permissions_per_month)
		max_minutes = cint(handbook.max_permission_minutes_per_month)
		if not (max_count or max_minutes):
			return
		start_day = handbook_start_day(handbook)
		checked = set()
		for d in iter_dates(self.from_date, self.to_date):
			if not svc.occurs_on(self, d):
				continue
			period = get_period_for_date(d, start_day)
			if period.label in checked:
				continue
			checked.add(period.label)
			count, minutes = svc.count_period_usage(self.employee, period.start, period.end, exclude=self.name)
			mine = sum(1 for x in iter_dates(max(period.start, getdate(self.from_date)), min(period.end, getdate(self.to_date))) if svc.occurs_on(self, x))
			if max_count and count + mine > max_count:
				frappe.throw(_("Only {0} permission(s) are allowed in the period {1} – {2}.").format(max_count, period.start, period.end))
			if max_minutes and minutes + mine * self.duration_minutes > max_minutes:
				frappe.throw(
					_("Permission time in the period {0} – {1} would be {2} min; the limit is {3} min.").format(
						period.start, period.end, minutes + mine * self.duration_minutes, max_minutes
					)
				)

	def before_submit(self):
		if self.status not in ("Approved", "Rejected"):
			frappe.throw(_("Only Approved or Rejected permissions can be submitted."))

	def on_cancel(self):
		self.db_set("status", "Cancelled")
