# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

"""Forgotten check-in / check-out request.

Approval (submit with status Approved):
1. If the day already has a submitted Attendance built from check-ins, it is cancelled so HRMS can
   rebuild it (HRMS re-processes check-ins that are not linked to an attendance). Draft Half Day
   Leave Deductions for that attendance are removed; a submitted one blocks approval.
2. The missing Employee Checkin is created (device_id "Check-in Request <name>").
3. HRMS auto attendance for the shift is queued, so the day is re-marked right away.
Cancelling an approved request deletes the created check-in (only while no attendance uses it).
"""

from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, get_time, getdate, now_datetime

from mobility_hr.hr_policy.handbook import get_handbook
from mobility_hr.hr_policy.periods import get_period_for_date, handbook_start_day

HR_ROLES = {"HR Manager", "HR User", "System Manager"}
DUPLICATE_SECONDS = 60


class CheckinRequest(Document):
	@property
	def punch_time(self) -> datetime:
		return datetime.combine(getdate(self.date), get_time(self.time))

	def validate(self):
		self.validate_time()
		self.validate_status_change()
		if self.status in ("Open", "Approved") and self.docstatus < 2:
			self.validate_duplicates()
			self.validate_limit()

	def validate_time(self):
		if self.punch_time > now_datetime():
			frappe.throw(_("The check-in time cannot be in the future."))
		joining = frappe.db.get_value("Employee", self.employee, "date_of_joining")
		if joining and getdate(self.date) < getdate(joining):
			frappe.throw(_("The date is before the employee's joining date ({0}).").format(joining))

	def validate_status_change(self):
		if self.is_new() and self.status != "Open" and not (HR_ROLES & set(frappe.get_roles())):
			self.status = "Open"
		if self.is_new() or not self.has_value_changed("status") or self.status == "Open":
			return
		user = frappe.session.user
		is_hr = bool(HR_ROLES & set(frappe.get_roles()))
		if not (is_hr or (self.approver and self.approver == user)):
			frappe.throw(_("Only the approver or HR can approve or reject a check-in request."))
		if frappe.db.get_value("Employee", self.employee, "user_id") == user and not is_hr:
			frappe.throw(_("You cannot approve your own check-in request."))
		self.approved_by = user
		self.approved_on = now_datetime()

	def validate_duplicates(self):
		window = (self.punch_time - timedelta(seconds=DUPLICATE_SECONDS), self.punch_time + timedelta(seconds=DUPLICATE_SECONDS))
		existing = frappe.db.get_value("Employee Checkin", {"employee": self.employee, "time": ["between", window]}, "name")
		if existing:
			frappe.throw(_("A check-in already exists at this time: {0}.").format(frappe.get_desk_link("Employee Checkin", existing)))
		others = frappe.get_all(
			"Checkin Request",
			filters={
				"employee": self.employee,
				"date": self.date,
				"name": ["!=", self.name or ""],
				"docstatus": ["<", 2],
				"status": ["in", ["Open", "Approved"]],
			},
			fields=["name", "time"],
		)
		for other in others:
			other_time = datetime.combine(getdate(self.date), get_time(other.time))
			if abs((other_time - self.punch_time).total_seconds()) < DUPLICATE_SECONDS:
				frappe.throw(_("Another request for the same time exists: {0}.").format(frappe.get_desk_link("Checkin Request", other.name)))

	def validate_limit(self):
		handbook = get_handbook(self.employee, self.date)
		limit = cint(handbook.get("max_checkin_requests_per_month")) if handbook else 0
		if not limit:
			return
		period = get_period_for_date(self.date, handbook_start_day(handbook))
		count = frappe.db.count(
			"Checkin Request",
			{
				"employee": self.employee,
				"name": ["!=", self.name or ""],
				"docstatus": ["<", 2],
				"status": ["in", ["Open", "Approved"]],
				"date": ["between", [period.start, period.end]],
			},
		)
		if count + 1 > limit:
			frappe.throw(
				_("Only {0} check-in request(s) are allowed between {1} and {2}.").format(limit, period.start, period.end)
			)

	def before_submit(self):
		if self.status not in ("Approved", "Rejected"):
			frappe.throw(_("Set the Approval Status to Approved or Rejected before submitting."))

	def on_submit(self):
		if self.status != "Approved":
			return
		notes = []
		attendance = self.reset_attendance(notes)
		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": self.employee,
				"time": self.punch_time,
				"log_type": self.log_type or None,
				"device_id": f"Check-in Request {self.name}",
			}
		)
		checkin.flags.ignore_permissions = True
		checkin.insert()
		self.queue_attendance(checkin, notes)
		self.db_set(
			{
				"employee_checkin": checkin.name,
				"attendance_reset": attendance,
				"result_note": " ".join(notes) or None,
			}
		)

	def reset_attendance(self, notes: list) -> str | None:
		att = frappe.db.get_value(
			"Attendance",
			{"employee": self.employee, "attendance_date": self.date, "docstatus": 1},
			["name", "leave_application", "status"],
			as_dict=True,
		)
		if not att:
			return None
		if att.leave_application:
			notes.append(_("Attendance {0} comes from a leave application and was not changed.").format(att.name))
			return None
		submitted = frappe.db.get_value("Half Day Leave Deduction", {"attendance": att.name, "docstatus": 1}, "name")
		if submitted:
			frappe.throw(
				_("Attendance {0} has an applied Half Day Leave Deduction {1}. Cancel the deduction first.").format(att.name, submitted)
			)
		for name in frappe.get_all("Half Day Leave Deduction", filters={"attendance": att.name, "docstatus": 0}, pluck="name"):
			frappe.delete_doc("Half Day Leave Deduction", name, ignore_permissions=True, force=True)
		doc = frappe.get_doc("Attendance", att.name)
		doc.flags.ignore_permissions = True
		doc.cancel()
		doc.add_comment("Info", _("Cancelled by approved {0} so the day is re-processed with the corrected punches.").format(
			frappe.get_desk_link("Checkin Request", self.name)))
		notes.append(_("Previous attendance {0} ({1}) was cancelled for re-processing.").format(att.name, att.status))
		return att.name

	def queue_attendance(self, checkin, notes: list):
		shift = frappe.db.get_value("Employee Checkin", checkin.name, "shift")
		if not shift:
			notes.append(_("No shift found for this time, so attendance is not re-marked automatically."))
			return
		st = frappe.db.get_value("Shift Type", shift, ["enable_auto_attendance", "process_attendance_after"], as_dict=True)
		if not st.enable_auto_attendance or (st.process_attendance_after and getdate(self.date) < getdate(st.process_attendance_after)):
			notes.append(_("Shift {0} does not auto-mark attendance for this date; mark it manually if needed.").format(shift))
			return
		frappe.enqueue(
			"mobility_hr.mobility_hr.doctype.checkin_request.checkin_request.process_shift_attendance",
			queue="long",
			shift=shift,
			job_id=f"checkin_request_attendance::{shift}",
			deduplicate=True,
			enqueue_after_commit=True,
		)
		notes.append(_("Attendance for shift {0} is being re-processed.").format(shift))

	def on_cancel(self):
		if not self.employee_checkin or not frappe.db.exists("Employee Checkin", self.employee_checkin):
			return
		attendance = frappe.db.get_value("Employee Checkin", self.employee_checkin, "attendance")
		if attendance and frappe.db.get_value("Attendance", attendance, "docstatus") == 1:
			frappe.throw(
				_("Check-in {0} is used by attendance {1}. Cancel that attendance first.").format(self.employee_checkin, attendance)
			)
		frappe.delete_doc("Employee Checkin", self.employee_checkin, ignore_permissions=True, force=True)


def process_shift_attendance(shift: str):
	"""Background: let HRMS mark attendance for the shift now (same as its hourly job)."""
	frappe.get_doc("Shift Type", shift).process_auto_attendance()
