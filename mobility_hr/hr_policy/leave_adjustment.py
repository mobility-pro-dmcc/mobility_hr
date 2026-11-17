"""Leave balance changes through supported HRMS transactions.

Deductions/grants change `new_leaves_allocated` on a *submitted* Leave Allocation and save it.
That is HRMS's own after-submit path (`LeaveAllocation.on_update_after_submit`): it validates
against approved leave applications, re-runs allocation validations and writes the Leave
Ledger Entry for the difference. No direct database updates are made.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on
from hrms.hr.utils import get_leave_period


class InsufficientLeaveBalance(frappe.ValidationError):
	pass


def get_allocation(employee: str, leave_type: str, on_date) -> str | None:
	return frappe.db.get_value(
		"Leave Allocation",
		{
			"employee": employee,
			"leave_type": leave_type,
			"docstatus": 1,
			"from_date": ["<=", getdate(on_date)],
			"to_date": [">=", getdate(on_date)],
		},
		"name",
		order_by="from_date desc",
	)


def get_balance(employee: str, leave_type: str, on_date) -> float:
	return flt(get_leave_balance_on(employee, leave_type, getdate(on_date), consider_all_leaves_in_the_allocation_period=True))


def change_allocation(allocation: str, delta: float, reference_doctype: str, reference_name: str, reason: str):
	doc = frappe.get_doc("Leave Allocation", allocation)
	doc.new_leaves_allocated = flt(doc.new_leaves_allocated) + flt(delta)
	doc.flags.ignore_permissions = True
	doc.save()
	verb = _("Added") if delta > 0 else _("Deducted")
	doc.add_comment(
		"Info",
		_("{0} {1} day(s) via {2} {3}: {4}").format(
			verb, abs(flt(delta)), _(reference_doctype), frappe.get_desk_link(reference_doctype, reference_name), reason
		),
	)
	return doc.name


def deduct(employee, leave_type, on_date, days, reference_doctype, reference_name, reason, allow_partial=False) -> dict:
	"""Deduct `days`. Returns {"allocation", "balance", "deducted"}; raises InsufficientLeaveBalance."""
	days = flt(days)
	allocation = get_allocation(employee, leave_type, on_date)
	balance = get_balance(employee, leave_type, on_date) if allocation else 0.0
	if not allocation or balance < days:
		if not allow_partial or balance <= 0 or not allocation:
			raise InsufficientLeaveBalance(
				_("Insufficient {0} balance: {1} available, {2} required.").format(leave_type, balance, days)
			)
		days = balance
	change_allocation(allocation, -days, reference_doctype, reference_name, reason)
	return {"allocation": allocation, "balance": balance, "deducted": days}


def grant(employee, leave_type, on_date, days, reference_doctype, reference_name, reason) -> str:
	"""Add `days` to the allocation covering `on_date`, creating one for the leave period if needed."""
	allocation = get_allocation(employee, leave_type, on_date)
	if allocation:
		return change_allocation(allocation, days, reference_doctype, reference_name, reason)

	company = frappe.db.get_value("Employee", employee, "company")
	period = get_leave_period(on_date, on_date, company)
	if period:
		from_date, to_date = getdate(on_date), getdate(period[0].to_date)
	else:
		d = getdate(on_date)
		from_date, to_date = d, d.replace(month=12, day=31)
	doc = frappe.get_doc(
		{
			"doctype": "Leave Allocation",
			"employee": employee,
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": to_date,
			"new_leaves_allocated": flt(days),
			"description": reason,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	doc.add_comment("Info", _("Created via {0} {1}").format(_(reference_doctype), frappe.get_desk_link(reference_doctype, reference_name)))
	return doc.name


def reverse(allocation: str, delta: float, reference_doctype: str, reference_name: str):
	"""Undo a previous change (used on cancel of the source document)."""
	if allocation and flt(delta) and frappe.db.get_value("Leave Allocation", allocation, "docstatus") == 1:
		change_allocation(allocation, -flt(delta), reference_doctype, reference_name, _("Reversed on cancellation"))
