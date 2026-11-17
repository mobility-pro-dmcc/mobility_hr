"""Employee HR API (web dashboard now, Android later).

Every method:
- resolves the employee from the logged-in user (`Employee.user_id`); an `employee` argument is
  only honoured for HR users who have read permission on that Employee;
- validates inputs; returns {"employee": ..., "period": ..., "data": ...};
- delegates all business logic to mobility_hr.hr_policy.dashboard.
Call with GET: /api/method/mobility_hr.api.hr.<method>?year=2026&month=11
"""

import frappe
from frappe import _
from frappe.utils import cint

from mobility_hr.hr_policy import dashboard

HR_ROLES = {"HR Manager", "HR User", "System Manager"}
MAX_PAGE_LENGTH = 100


def resolve_employee(employee: str | None = None) -> str:
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Login required."))
	own = frappe.db.get_value("Employee", {"user_id": user, "status": ["!=", "Left"]}, "name")
	if employee and employee != own:
		if not (HR_ROLES & set(frappe.get_roles(user))) or not frappe.has_permission("Employee", "read", doc=employee, user=user):
			raise frappe.PermissionError(_("You are not allowed to view this employee's information."))
		if not frappe.db.exists("Employee", employee):
			raise frappe.DoesNotExistError(_("Employee {0} not found.").format(employee))
		return employee
	if not own:
		raise frappe.PermissionError(_("No active Employee record is linked to your user."))
	return own


def _period_args(year, month) -> tuple[int | None, int | None]:
	if year in (None, "") and month in (None, ""):
		return None, None
	try:
		year, month = int(year), int(month)
	except (TypeError, ValueError):
		frappe.throw(_("year and month must be numbers."), frappe.ValidationError)
	if not (2000 <= year <= 2100 and 1 <= month <= 12):
		frappe.throw(_("Invalid year or month."), frappe.ValidationError)
	return year, month


def _context(employee, year, month):
	emp = resolve_employee(employee)
	period, handbook = dashboard.resolve_period(emp, *_period_args(year, month))
	return emp, period, handbook


def _response(emp, period, data) -> dict:
	info = frappe.db.get_value("Employee", emp, ["name", "employee_name", "department", "designation", "company"], as_dict=True)
	return {"employee": info, "period": period.as_dict() if period else None, "data": data}


@frappe.whitelist(methods=["GET"])
def get_dashboard(year=None, month=None, employee=None):
	"""Summary cards + daily table in one call (what the web dashboard uses)."""
	emp, period, handbook = _context(employee, year, month)
	days = dashboard.build_days(emp, period)
	return _response(
		emp,
		period,
		{
			"handbook": handbook.name if handbook else None,
			"overtime_enabled": dashboard.overtime_applies(handbook),
			"summary": dashboard.get_summary(emp, period, handbook, days),
			"days": dashboard.public_days(days),
		},
	)


@frappe.whitelist(methods=["GET"])
def get_summary(year=None, month=None, employee=None):
	emp, period, handbook = _context(employee, year, month)
	return _response(emp, period, dashboard.get_summary(emp, period, handbook))


@frappe.whitelist(methods=["GET"])
def get_attendance(year=None, month=None, employee=None):
	emp, period, _hb = _context(employee, year, month)
	return _response(emp, period, dashboard.public_days(dashboard.build_days(emp, period)))


@frappe.whitelist(methods=["GET"])
def get_leave_balances(employee=None):
	emp = resolve_employee(employee)
	return _response(emp, None, dashboard.get_leave_balances(emp))


@frappe.whitelist(methods=["GET"])
def get_deductions(year=None, month=None, employee=None):
	emp, period, _hb = _context(employee, year, month)
	return _response(emp, period, dashboard.get_deductions(emp, period))


@frappe.whitelist(methods=["GET"])
def get_overtime(year=None, month=None, employee=None):
	emp, period, _hb = _context(employee, year, month)
	return _response(emp, period, dashboard.get_overtime(emp, period))


@frappe.whitelist(methods=["GET"])
def get_permissions(year=None, month=None, start=0, page_length=20, employee=None):
	emp, period, _hb = _context(employee, year, month)
	start, page_length = max(cint(start), 0), min(max(cint(page_length), 1), MAX_PAGE_LENGTH)
	return _response(emp, period, dashboard.get_permissions(emp, period, start, page_length))


@frappe.whitelist(methods=["GET"])
def get_leave_policies(employee=None):
	emp = resolve_employee(employee)
	return _response(emp, None, {"balances": dashboard.get_leave_balances(emp), "policies": dashboard.get_leave_policies(emp)})


@frappe.whitelist(methods=["POST"])
def create_checkin_request(date, time, reason, log_type=None):
	"""Employee: request a forgotten check-in/out for themselves. HR approves it in the desk."""
	from mobility_hr.hr_policy import checkin_requests

	emp = resolve_employee()
	if not reason or not str(reason).strip():
		frappe.throw(_("Please give a reason."), frappe.ValidationError)
	if log_type not in (None, "", "IN", "OUT"):
		frappe.throw(_("log_type must be IN or OUT."), frappe.ValidationError)
	try:
		frappe.utils.getdate(date)
		frappe.utils.get_time(time)
	except Exception:
		frappe.throw(_("Invalid date or time."), frappe.ValidationError)
	return _response(emp, None, checkin_requests.create(emp, date, time, str(reason).strip()[:500], log_type))


@frappe.whitelist(methods=["GET"])
def get_checkin_requests(start=0, page_length=20, employee=None):
	from mobility_hr.hr_policy import checkin_requests

	emp = resolve_employee(employee)
	start, page_length = max(cint(start), 0), min(max(cint(page_length), 1), MAX_PAGE_LENGTH)
	return _response(emp, None, checkin_requests.list_requests(emp, start, page_length))


# --------------------------------------------------------------------------- self-service requests


def _text(value, label, max_len=500):
	value = str(value or "").strip()
	if not value:
		frappe.throw(_("{0} is required.").format(label), frappe.ValidationError)
	return value[:max_len]


@frappe.whitelist(methods=["GET"])
def get_request_options():
	from mobility_hr.hr_policy import self_service

	emp = resolve_employee()
	return _response(emp, None, self_service.request_options(emp))


@frappe.whitelist(methods=["GET"])
def get_my_requests(limit=30):
	from mobility_hr.hr_policy import self_service

	emp = resolve_employee()
	return _response(emp, None, self_service.list_requests(emp, min(max(cint(limit), 1), MAX_PAGE_LENGTH)))


@frappe.whitelist(methods=["POST"])
def create_leave_application(leave_type, from_date, to_date, reason, half_day=0, half_day_date=None):
	from mobility_hr.hr_policy import self_service

	emp = resolve_employee()
	if not frappe.db.exists("Leave Type", leave_type):
		frappe.throw(_("Unknown leave type."), frappe.ValidationError)
	return _response(
		emp, None, self_service.create_leave_application(emp, leave_type, from_date, to_date, _text(reason, _("Reason")), half_day, half_day_date)
	)


@frappe.whitelist(methods=["POST"])
def create_permission_request(permission_type, date, start_time, end_time, reason):
	from mobility_hr.hr_policy import self_service

	emp = resolve_employee()
	if permission_type not in ("Late Arrival", "Early Departure", "Temporary Absence"):
		frappe.throw(_("Unknown permission type."), frappe.ValidationError)
	return _response(emp, None, self_service.create_permission(emp, permission_type, date, start_time, end_time, _text(reason, _("Reason"))))


@frappe.whitelist(methods=["POST"])
def withdraw_request(doctype, name):
	from mobility_hr.hr_policy import self_service

	emp = resolve_employee()
	self_service.withdraw(emp, doctype, name)
	return _response(emp, None, {"withdrawn": name})


@frappe.whitelist(methods=["GET"])
def get_permitted_employees():
	"""HR employee picker: employees the current user may read (Frappe roles + user permissions)."""
	if not (HR_ROLES & set(frappe.get_roles())):
		return {"employee": None, "period": None, "data": []}
	rows = frappe.get_list(
		"Employee",
		filters={"status": ["!=", "Left"]},
		fields=["name", "employee_name", "department", "designation", "company"],
		order_by="employee_name asc",
		limit_page_length=2000,
	)
	return {"employee": None, "period": None, "data": rows}
