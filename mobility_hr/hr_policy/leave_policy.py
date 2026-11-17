"""Handbook leave rules, applied on top of HRMS's own Leave Application validations.

HRMS already enforces: balance, overlap, Leave Type max continuous days / applicable after,
block lists, attendance conflicts. The handbook adds per-group rules HRMS does not have
(advance notice, request counts per period, yearly caps, request duration, service,
probation, mandatory attachment). "Override Handbook Policy" lets an HR Manager bypass them;
the bypass is recorded as a comment.
"""

from datetime import date

import frappe
from dateutil.relativedelta import relativedelta
from frappe import _
from frappe.utils import date_diff, flt, getdate

from mobility_hr.hr_policy.handbook import get_employee, get_handbook
from mobility_hr.hr_policy.periods import get_period_for_date, handbook_start_day

ACTIVE_STATUSES = ("Open", "Approved")


class HandbookLeavePolicyError(frappe.ValidationError):
	pass


def get_rule(handbook, leave_type: str):
	if not handbook:
		return None
	return next((r for r in handbook.leave_policy_rules or [] if r.enabled and r.leave_type == leave_type), None)


def in_probation(emp, on_date) -> bool:
	end = emp.final_confirmation_date or emp.scheduled_confirmation_date
	return bool(end) and getdate(on_date) < getdate(end)


def count_window(rule, handbook, d) -> tuple[date, date]:
	d = getdate(d)
	if rule.request_limit_period == "Calendar Year":
		return date(d.year, 1, 1), date(d.year, 12, 31)
	if rule.request_limit_period == "Calendar Month":
		start = date(d.year, d.month, 1)
		return start, start + relativedelta(months=1, days=-1)
	p = get_period_for_date(d, handbook_start_day(handbook))
	return p.start, p.end


def other_applications(doc, start, end) -> list:
	return frappe.get_all(
		"Leave Application",
		filters={
			"employee": doc.employee,
			"leave_type": doc.leave_type,
			"name": ["!=", doc.name or ""],
			"docstatus": ["<", 2],
			"status": ["in", ACTIVE_STATUSES],
			"from_date": ["between", [start, end]],
		},
		fields=["name", "total_leave_days"],
	)


def get_violations(doc, handbook, rule) -> list[str]:
	emp = get_employee(doc.employee)
	errors = []
	days = flt(doc.total_leave_days)
	posting = getdate(doc.posting_date or frappe.utils.today())

	if rule.min_advance_notice_days:
		notice = date_diff(doc.from_date, posting)
		if notice < rule.min_advance_notice_days:
			errors.append(
				_("{0} must be requested at least {1} day(s) in advance (requested {2} day(s) before).").format(
					doc.leave_type, rule.min_advance_notice_days, notice
				)
			)

	if flt(rule.min_request_days) and days < flt(rule.min_request_days):
		errors.append(_("Minimum duration for {0} is {1} day(s).").format(doc.leave_type, flt(rule.min_request_days)))
	if flt(rule.max_request_days) and days > flt(rule.max_request_days):
		errors.append(_("Maximum duration for {0} is {1} day(s) per request.").format(doc.leave_type, flt(rule.max_request_days)))

	if flt(rule.max_consecutive_days):
		consecutive = days
		if hasattr(doc, "get_consecutive_leave_details"):
			details = doc.get_consecutive_leave_details() or {}
			consecutive = flt(details.get("total_consecutive_leaves", days)) if isinstance(details, dict) else days
		if consecutive > flt(rule.max_consecutive_days):
			errors.append(
				_("{0} cannot exceed {1} consecutive day(s) (this request makes {2}).").format(
					doc.leave_type, flt(rule.max_consecutive_days), consecutive
				)
			)

	if rule.max_requests_per_period:
		start, end = count_window(rule, handbook, doc.from_date)
		count = len(other_applications(doc, start, end)) + 1
		if count > rule.max_requests_per_period:
			errors.append(
				_("Only {0} {1} request(s) are allowed between {2} and {3}.").format(
					rule.max_requests_per_period, doc.leave_type, start, end
				)
			)

	if flt(rule.max_days_per_year):
		y = getdate(doc.from_date).year
		used = sum(flt(a.total_leave_days) for a in other_applications(doc, date(y, 1, 1), date(y, 12, 31)))
		if used + days > flt(rule.max_days_per_year):
			errors.append(
				_("{0} is limited to {1} day(s) per year; {2} already requested.").format(
					doc.leave_type, flt(rule.max_days_per_year), used
				)
			)

	if rule.min_service_months and emp.date_of_joining:
		served = relativedelta(getdate(doc.from_date), getdate(emp.date_of_joining))
		months = served.years * 12 + served.months
		if months < rule.min_service_months:
			errors.append(
				_("{0} is available after {1} month(s) of service ({2} completed).").format(
					doc.leave_type, rule.min_service_months, months
				)
			)

	if not rule.allow_during_probation and in_probation(emp, doc.from_date):
		errors.append(_("{0} is not allowed during probation.").format(doc.leave_type))

	if rule.require_attachment and doc.status == "Approved" and not doc.is_new():
		if not frappe.db.exists("File", {"attached_to_doctype": doc.doctype, "attached_to_name": doc.name}):
			errors.append(_("{0} requires a supporting document. Attach it before approval.").format(doc.leave_type))
	return errors


def validate_leave_application(doc, method=None):
	if doc.status in ("Rejected", "Cancelled") or doc.docstatus == 2:
		return
	handbook = get_handbook(doc.employee, doc.from_date)
	rule = get_rule(handbook, doc.leave_type)
	if not rule:
		return

	errors = get_violations(doc, handbook, rule)
	if not errors:
		return

	if doc.get("override_handbook_policy"):
		if "HR Manager" not in frappe.get_roles():
			frappe.throw(_("Only an HR Manager can override the handbook leave policy."), HandbookLeavePolicyError)
		if not doc.is_new() and doc.has_value_changed("override_handbook_policy"):
			doc.add_comment("Info", _("Handbook policy overridden by {0}: {1}").format(frappe.session.user, "; ".join(errors)))
		return

	frappe.throw(
		"<br>".join(errors) + "<br><br>" + _("Policy: HR Handbook {0}").format(frappe.get_desk_link("HR Handbook", handbook.name)),
		HandbookLeavePolicyError,
		title=_("Leave Policy"),
	)


# --------------------------------------------------------------------------- linked deduction
# e.g. Casual / Emergency Leave "to be deducted from annual leave": the application consumes its
# own leave type (with its own yearly cap) AND the same days from the linked leave type.


def deduct_linked_leave(doc, method=None):
	"""Leave Application.on_submit (approved)."""
	if doc.status != "Approved":
		return
	from mobility_hr.hr_policy import leave_adjustment

	rule = get_rule(get_handbook(doc.employee, doc.from_date), doc.leave_type)
	if not rule or not rule.get("deduct_from_leave_type") or not flt(doc.total_leave_days):
		return
	try:
		result = leave_adjustment.deduct(
			doc.employee,
			rule.deduct_from_leave_type,
			doc.from_date,
			flt(doc.total_leave_days),
			doc.doctype,
			doc.name,
			_("{0} is deducted from {1} per the HR Handbook.").format(doc.leave_type, rule.deduct_from_leave_type),
		)
	except leave_adjustment.InsufficientLeaveBalance as e:
		frappe.throw(_("{0} {1} is also deducted from {2}.").format(e, doc.leave_type, rule.deduct_from_leave_type), title=_("Leave Policy"))
	doc.db_set({"handbook_deducted_allocation": result["allocation"], "handbook_deducted_days": result["deducted"]})


def reverse_linked_leave(doc, method=None):
	"""Leave Application.on_cancel."""
	if doc.get("handbook_deducted_allocation") and flt(doc.get("handbook_deducted_days")):
		from mobility_hr.hr_policy import leave_adjustment

		leave_adjustment.reverse(doc.handbook_deducted_allocation, -flt(doc.handbook_deducted_days), doc.doctype, doc.name)
