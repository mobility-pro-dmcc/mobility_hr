"""Custom fields that extend standard HRMS DocTypes. Applied on every migrate (idempotent)."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Shift Type": [
		{
			"fieldname": "wfh_section",
			"fieldtype": "Section Break",
			"label": "Work From Home",
			"insert_after": "overtime_type",
			"collapsible": 1,
		},
		{
			"fieldname": "wfh_attendance_rule",
			"fieldtype": "Select",
			"label": "WFH Attendance Rule",
			"options": "Require Check-in\nMark Automatically",
			"default": "Require Check-in",
			"insert_after": "wfh_section",
			"description": "Require Check-in: attendance from check-ins on a WFH day is marked 'Work From Home'; "
			"no check-in stays Absent. Mark Automatically: the employee is marked 'Work From Home' without check-ins "
			"(never on holidays, approved leave, or days that already have attendance).",
		},
		{
			"fieldname": "wfh_days",
			"fieldtype": "Table",
			"label": "Work From Home Days",
			"options": "Shift Work From Home Day",
			"insert_after": "wfh_attendance_rule",
		},
	],
	"Attendance": [
		{
			"fieldname": "policy_note",
			"fieldtype": "Small Text",
			"label": "Policy Classification",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "early_exit",
			"description": "Why the HR Handbook / Shift WFH rules set this status.",
		}
	],
	"Leave Allocation": [
		{
			"fieldname": "hr_handbook",
			"fieldtype": "Link",
			"label": "HR Handbook",
			"options": "HR Handbook",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "leave_policy_assignment",
		},
		{
			"fieldname": "handbook_auto_allocated",
			"fieldtype": "Check",
			"label": "Allocated Automatically by Handbook",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "hr_handbook",
		},
	],
	"Leave Application": [
		{
			"fieldname": "override_handbook_policy",
			"fieldtype": "Check",
			"label": "Override Handbook Policy",
			"no_copy": 1,
			"insert_after": "follow_via_email",
			"depends_on": "eval:frappe.user.has_role('HR Manager')",
			"description": "HR Manager only: approve even though the HR Handbook leave rules are not met. Recorded as a comment.",
		},
		{
			"fieldname": "handbook_deducted_allocation",
			"fieldtype": "Link",
			"label": "Also Deducted From Allocation",
			"options": "Leave Allocation",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "override_handbook_policy",
			"depends_on": "handbook_deducted_allocation",
		},
		{
			"fieldname": "handbook_deducted_days",
			"fieldtype": "Float",
			"label": "Days Also Deducted",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "handbook_deducted_allocation",
			"depends_on": "handbook_deducted_allocation",
		},
	],
}


SELF_SERVICE_DOCTYPES = ("Checkin Request", "Employee Permission")


def after_migrate():
	from mobility_hr.branding import apply_branding

	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)
	add_to_employee_self_service()
	apply_branding()


def add_to_employee_self_service():
	"""Let 'Employee Self Service' users create their own permission and check-in requests (not approve)."""
	import frappe

	if not frappe.db.exists("User Type", "Employee Self Service"):
		return
	user_type = frappe.get_doc("User Type", "Employee Self Service")
	existing = {d.document_type for d in user_type.user_doctypes}
	missing = [dt for dt in SELF_SERVICE_DOCTYPES if dt not in existing and frappe.db.exists("DocType", dt)]
	if not missing:
		return
	for dt in missing:
		user_type.append("user_doctypes", {"document_type": dt, "read": 1, "write": 1, "create": 1})
	user_type.flags.ignore_permissions = True
	user_type.save()


def before_uninstall():
	"""Remove what this app added to standard DocTypes, so nothing points at deleted DocTypes."""
	import frappe

	for doctype, fields in CUSTOM_FIELDS.items():
		for field in fields:
			name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": field["fieldname"]})
			if name:
				frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
	if frappe.db.exists("User Type", "Employee Self Service"):
		frappe.db.delete("User Document Type", {"parent": "Employee Self Service", "document_type": ["in", list(SELF_SERVICE_DOCTYPES)]})
	frappe.db.delete("Property Setter", {"doc_type": "User", "field_name": "desk_theme", "property": "default"})
	for name in frappe.get_all("Scheduled Job Type", filters={"method": ["like", "mobility_hr.%"]}, pluck="name"):
		frappe.delete_doc("Scheduled Job Type", name, ignore_permissions=True, force=True)
	frappe.clear_cache()
