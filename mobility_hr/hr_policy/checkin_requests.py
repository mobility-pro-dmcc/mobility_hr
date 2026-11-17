"""Service for forgotten check-in requests (used by the web dashboard and the API)."""

import frappe
from frappe.utils import get_time, getdate


def create(employee: str, date, time, reason: str, log_type: str | None = None) -> dict:
	doc = frappe.get_doc(
		{
			"doctype": "Checkin Request",
			"employee": employee,
			"date": getdate(date),
			"time": get_time(time),
			"log_type": log_type or None,
			"reason": reason,
			"status": "Open",
		}
	)
	doc.insert(ignore_permissions=True)  # authorization is done by the API (own employee only)
	return {"name": doc.name, "status": doc.status}


def list_requests(employee: str, start: int = 0, page_length: int = 20) -> dict:
	filters = {"employee": employee, "docstatus": ["<", 2]}
	rows = frappe.get_all(
		"Checkin Request",
		filters=filters,
		fields=["name", "date", "time", "log_type", "reason", "status", "docstatus", "employee_checkin"],
		order_by="date desc, time desc",
		start=start,
		page_length=page_length,
	)
	for r in rows:
		r["time"] = str(r["time"])[:5]
		r["state"] = "Approved" if r.status == "Approved" and r.docstatus == 1 else ("Rejected" if r.status == "Rejected" else "Pending Approval")
		r.pop("docstatus")
	return {"records": rows, "total": frappe.db.count("Checkin Request", filters), "start": start, "page_length": page_length}
