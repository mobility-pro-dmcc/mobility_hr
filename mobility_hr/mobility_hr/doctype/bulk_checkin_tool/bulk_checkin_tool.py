# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

"""HR bulk check-in: one approved Check-in Request per selected employee.

Each request goes through the normal Check-in Request approval, so the check-in is created,
that day's attendance is re-processed, and every entry stays traceable (Source = HR Bulk Entry).
One employee failing (e.g. a punch already exists) never stops the others.
"""

import frappe
from frappe import _
from frappe.model.document import Document

HR_ROLES = ("HR Manager", "HR User", "System Manager")
BACKGROUND_THRESHOLD = 25


class BulkCheckinTool(Document):
	@frappe.whitelist()
	def get_employees(self):
		"""Active employees matching the filters (added to the selection by the form)."""
		frappe.only_for(HR_ROLES)
		filters = {"status": "Active"}
		for field, emp_field in (("company", "company"), ("department", "department"), ("branch", "branch"), ("shift", "default_shift")):
			if self.get(field):
				filters[emp_field] = self.get(field)
		return frappe.get_all("Employee", filters=filters, fields=["name", "employee_name"], order_by="employee_name")

	@frappe.whitelist()
	def create_checkins(self):
		frappe.only_for(HR_ROLES)
		employees = list(dict.fromkeys(row.employee for row in self.employees if row.employee))
		if not employees:
			frappe.throw(_("Select at least one employee."))
		if not (self.date and self.time and (self.reason or "").strip()):
			frappe.throw(_("Date, Time and Reason are required."))
		args = dict(employees=employees, date=self.date, time=self.time, log_type=self.log_type, reason=self.reason.strip())
		if len(employees) > BACKGROUND_THRESHOLD:
			frappe.enqueue(
				"mobility_hr.mobility_hr.doctype.bulk_checkin_tool.bulk_checkin_tool.run",
				queue="long",
				timeout=3600,
				user=frappe.session.user,
				**args,
			)
			return {"queued": True, "count": len(employees)}
		return run(**args)


def run(employees, date, time, log_type, reason, user=None) -> dict:
	results = []
	for employee in employees:
		frappe.db.savepoint("bulk_checkin")
		try:
			req = frappe.get_doc(
				{
					"doctype": "Checkin Request",
					"employee": employee,
					"date": date,
					"time": time,
					"log_type": log_type or None,
					"reason": reason,
					"status": "Approved",
					"source": "HR Bulk Entry",
					"approved_by": frappe.session.user,
					"approved_on": frappe.utils.now_datetime(),
				}
			)
			req.flags.ignore_permissions = True
			req.insert()
			req.submit()
			results.append({"employee": employee, "ok": True, "request": req.name, "checkin": req.employee_checkin, "note": req.result_note or ""})
		except Exception as e:
			frappe.db.rollback(save_point="bulk_checkin")
			frappe.clear_last_message()
			results.append({"employee": employee, "ok": False, "error": frappe.utils.strip_html(str(e)) or e.__class__.__name__})
	frappe.db.commit()

	summary = {"created": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results), "results": results}
	if user:
		frappe.publish_realtime("bulk_checkin_done", summary, user=user, after_commit=True)
	return summary

