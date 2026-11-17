# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from mobility_hr.hr_policy import leave_adjustment


class AttendancePenalty(Document):
	def before_submit(self):
		if self.penalty_type != "Leave Days" or not flt(self.penalty_value):
			self.deduction_status = _("Recorded for payroll") if self.penalty_type != "Warning Only" else _("Warning")
			return
		handbook = frappe.get_cached_doc("HR Handbook", self.handbook) if self.handbook else None
		partial = bool(handbook and handbook.insufficient_balance_action == "Deduct Available Balance")
		try:
			result = leave_adjustment.deduct(
				self.employee,
				self.leave_type,
				self.period_end,
				flt(self.penalty_value),
				self.doctype,
				self.name,
				self.explanation or "",
				allow_partial=partial,
			)
		except leave_adjustment.InsufficientLeaveBalance as e:
			frappe.throw(
				_("{0} Keep this penalty as a draft for HR review, or cancel it.").format(e),
				title=_("Insufficient Leave Balance"),
			)
		self.leave_allocation = result["allocation"]
		self.leave_days_deducted = result["deducted"]
		self.deduction_status = (
			_("Deducted") if flt(result["deducted"]) >= flt(self.penalty_value) else _("Partially deducted (balance {0})").format(result["balance"])
		)

	def on_cancel(self):
		if self.leave_allocation and flt(self.leave_days_deducted):
			leave_adjustment.reverse(self.leave_allocation, -flt(self.leave_days_deducted), self.doctype, self.name)
			self.db_set("deduction_status", _("Reversed"))
