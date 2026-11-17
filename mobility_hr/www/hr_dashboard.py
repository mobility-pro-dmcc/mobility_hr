import os
import re

import frappe
from frappe import _
from frappe.utils.jinja_globals import is_rtl

no_cache = 1

# Values that reach the page as data (statuses, day names…) and are translated in the browser with __().
DATA_STRINGS = (
	"Present", "Absent", "Half Day", "On Leave", "Work From Home", "Not Processed", "Not Marked",
	"Weekly Off", "Public Holiday", "Not Employed", "Upcoming", "At work", "Not checked in",
	"Approved", "Applied", "Rejected", "Cancelled", "Pending Approval", "Open", "Draft",
	"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
	"Leave", "Permission", "Check-in", "Check-in Request", "Leave Application", "Employee Permission",
	"Late Arrival", "Early Departure", "Temporary Absence",
	"Weekly", "Monthly", "Per Attendance Period", "Days", "Hours", "Working Day", "Holiday",
)
_JS_STRING = re.compile(r'__\("((?:[^"\\]|\\.)*)"')


def page_strings() -> list[str]:
	"""Every __("…") literal in the page template, plus DATA_STRINGS."""
	path = os.path.join(os.path.dirname(__file__), "hr-dashboard.html")
	with open(path, encoding="utf-8") as f:
		found = _JS_STRING.findall(f.read())
	return sorted(set(found) | set(DATA_STRINGS))


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/hr-dashboard"
		raise frappe.Redirect
	from frappe.sessions import get_csrf_token

	from mobility_hr.branding import MODE_KEY, company_brand, mode_script, permitted_companies, style_tag

	context.no_cache = 1
	context.csrf_token = get_csrf_token()
	context.full_name = frappe.utils.get_fullname(frappe.session.user)
	context.is_hr = bool({"HR Manager", "HR User", "System Manager"} & set(frappe.get_roles()))
	context.has_desk = frappe.get_cached_value("User", frappe.session.user, "user_type") == "System User"
	emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user, "status": ["!=", "Left"]}, ["name", "company"], as_dict=True)
	context.employee = emp.name if emp else None

	# Name/logo/colours come from Company / Company Theme — never hard-coded.
	context.brand = company_brand(emp.company if emp else None)
	companies = permitted_companies() if context.is_hr else [context.brand]
	context.companies = companies or [context.brand]
	context.brand_style = style_tag(context.brand["company"])
	context.mode_script = mode_script()
	context.mode_key = MODE_KEY

	# Language (English / Arabic): Frappe has already resolved it from the user / cookie.
	context.lang = frappe.local.lang or "en"
	context.rtl = is_rtl()
	context.messages = {s: _(s) for s in page_strings() if _(s) != s}
	context.title = _("Employee Portal")
	return context
