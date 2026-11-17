# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import flt

from mobility_hr.hr_policy import leave_adjustment


class EmployeeOvertime(Document):
	def on_submit(self):
		"""Approval: grant compensatory leave through a Leave Allocation (HRMS ledger)."""
		if self.comp_leave_type and flt(self.comp_leave_days) > 0:
			allocation = leave_adjustment.grant(
				self.employee,
				self.comp_leave_type,
				self.attendance_date,
				flt(self.comp_leave_days),
				self.doctype,
				self.name,
				f"Holiday work on {self.attendance_date} ({self.day_type})",
			)
			self.db_set("leave_allocation", allocation)

	def on_cancel(self):
		if self.leave_allocation and flt(self.comp_leave_days):
			leave_adjustment.reverse(self.leave_allocation, flt(self.comp_leave_days), self.doctype, self.name)
