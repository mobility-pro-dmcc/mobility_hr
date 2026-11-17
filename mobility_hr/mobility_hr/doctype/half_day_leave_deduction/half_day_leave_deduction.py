# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from mobility_hr.hr_policy import leave_adjustment


class HalfDayLeaveDeduction(Document):
	def validate(self):
		if frappe.db.get_value("Attendance", self.attendance, "employee") != self.employee:
			frappe.throw(_("Attendance {0} does not belong to {1}.").format(self.attendance, self.employee))
		dup = frappe.db.exists(
			"Half Day Leave Deduction",
			{"attendance": self.attendance, "name": ["!=", self.name], "docstatus": ["<", 2]},
		)
		if dup:
			frappe.throw(_("Attendance {0} already has deduction {1}.").format(self.attendance, dup))

	def before_submit(self):
		handbook = frappe.get_cached_doc("HR Handbook", self.handbook) if self.handbook else None
		partial = bool(handbook and handbook.insufficient_balance_action == "Deduct Available Balance")
		try:
			result = leave_adjustment.deduct(
				self.employee,
				self.leave_type,
				self.attendance_date,
				flt(self.deduction_days),
				self.doctype,
				self.name,
				self.explanation or "",
				allow_partial=partial,
			)
		except leave_adjustment.InsufficientLeaveBalance as e:
			self.db_set("deduction_status", "Insufficient Balance")
			frappe.throw(_("{0} The deduction stays as a draft for HR review.").format(e), title=_("Insufficient Leave Balance"))
		self.leave_allocation = result["allocation"]
		self.available_balance = result["balance"]
		self.deducted_days = result["deducted"]
		self.deduction_status = "Applied" if flt(result["deducted"]) >= flt(self.deduction_days) else "Partially Applied"

	def on_cancel(self):
		if self.leave_allocation and flt(self.deducted_days):
			leave_adjustment.reverse(self.leave_allocation, -flt(self.deducted_days), self.doctype, self.name)
		self.db_set("deduction_status", "Reversed")
