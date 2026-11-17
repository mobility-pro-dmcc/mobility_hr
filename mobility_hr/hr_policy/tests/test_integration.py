"""Integration tests: hooks, jobs, controllers and API against real HRMS records.

Run inside one transaction that is rolled back (see the runner). Each test works inside a
savepoint, so tests do not affect each other.

Calendar used (October 2026): Fridays are weekly offs, Wed 7 Oct is a public holiday,
Tuesday is a Work From Home day of the test shift (09:00–17:00, 5 min grace).
"""

import unittest
from datetime import date, datetime

import frappe
from frappe.utils import flt

from mobility_hr.api import hr as api
from mobility_hr.hr_policy import day_facts, jobs, leave_adjustment, leave_entitlement, leave_policy, records, wfh
from mobility_hr.hr_policy.handbook import clear_request_cache, get_handbook_name

P = "_T HRP "
AS_OF = date(2026, 11, 1)


def ts(d, t):
	return datetime.fromisoformat(f"{d} {t}")


class HRPolicyIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_value("Company", {}, "name")
		cls.annual = cls.leave_type(P + "Annual")
		cls.comp = cls.leave_type(P + "Comp Off", is_compensatory=1)

		if frappe.db.exists("Holiday List", P + "HL"):
			# fixtures already created by another test class in this (rolled-back) run
			cls.holiday_list = P + "HL"
			cls.shift, cls.shift2 = P + "Shift", P + "Shift Late"
			cls.employee = frappe.db.get_value("Employee", {"first_name": P + "Employee"}, "name")
			cls.handbook, cls.company_handbook = P + "Handbook", P + "Company Wide"
			return

		hl = frappe.get_doc({"doctype": "Holiday List", "holiday_list_name": P + "HL", "from_date": "2026-01-01", "to_date": "2026-12-31"})
		for d in (2, 9, 16, 23, 30):
			hl.append("holidays", {"holiday_date": f"2026-10-{d:02d}", "description": "Friday", "weekly_off": 1})
		hl.append("holidays", {"holiday_date": "2026-10-07", "description": "Public Holiday", "weekly_off": 0})
		hl.insert()
		cls.holiday_list = hl.name

		cls.shift = cls.make_shift(P + "Shift", "09:00:00", "17:00:00", wfh=["Tuesday"])
		cls.shift2 = cls.make_shift(P + "Shift Late", "10:00:00", "18:00:00")

		gender = frappe.db.get_value("Gender", {}, "name")
		emp = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": P + "Employee",
				"gender": gender,
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2024-06-01",
				"company": cls.company,
				"holiday_list": hl.name,
				"default_shift": cls.shift,
				"status": "Active",
			}
		).insert()
		cls.employee = emp.name

		cls.handbook = cls.make_handbook()
		cls.company_handbook = frappe.get_doc(
			{
				"doctype": "HR Handbook",
				"handbook_name": P + "Company Wide",
				"company": cls.company,
				"priority": 50,
				"effective_from": "2025-01-01",
				"applies_to_all_employees": 1,
			}
		).insert().name

	@classmethod
	def tearDownClass(cls):
		for dt, name in (("HR Handbook", P + "Handbook"), ("HR Handbook", P + "Company Wide"), ("Shift Type", P + "Shift"), ("Shift Type", P + "Shift Late")):
			frappe.clear_document_cache(dt, name)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("hrp_test")
		clear_request_cache()
		day_facts.clear_cache()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="hrp_test")
		clear_request_cache()
		day_facts.clear_cache()
		frappe.clear_document_cache("HR Handbook", P + "Handbook")
		frappe.clear_document_cache("Shift Type", P + "Shift")

	# ------------------------------------------------------------------ fixtures

	@staticmethod
	def leave_type(name, **kw):
		if not frappe.db.exists("Leave Type", name):
			frappe.get_doc({"doctype": "Leave Type", "leave_type_name": name, "max_leaves_allowed": 0, "allow_over_allocation": 1, **kw}).insert()
		return name

	@staticmethod
	def make_shift(name, start, end, wfh=()):
		doc = frappe.get_doc(
			{
				"doctype": "Shift Type",
				"name": name,
				"start_time": start,
				"end_time": end,
				"late_entry_grace_period": 5,
				"enable_auto_attendance": 0,
				"determine_check_in_and_check_out": "Alternating entries as IN and OUT during the same shift",
				"working_hours_calculation_based_on": "Every Valid Check-in and Check-out",
				"begin_check_in_before_shift_start_time": 60,
				"allow_check_out_after_shift_end_time": 240,
				"wfh_attendance_rule": "Require Check-in",
				"wfh_days": [{"weekday": d} for d in wfh],
			}
		)
		doc.insert()
		return doc.name

	@classmethod
	def make_handbook(cls):
		rule = lambda **kw: {"enabled": 1, **kw}  # noqa: E731
		return frappe.get_doc(
			{
				"doctype": "HR Handbook",
				"handbook_name": P + "Handbook",
				"company": cls.company,
				"priority": 100,
				"effective_from": "2025-01-01",
				"applies_to_all_employees": 0,
				"applicability": [{"applicable_on": "Employee", "value": cls.employee}],
				"week_start_day": "Saturday",
				"enable_late_penalties": 1,
				"late_grace_source": "Handbook Grace Period",
				"late_grace_minutes": 5,
				"grace_handling": "Ignore lateness within grace, count full minutes beyond",
				"late_penalty_requires_approval": 0,
				"late_penalty_rules": [
					rule(rule_name="Daily 15-30", frequency="Daily", condition="Between", min_minutes=15, max_minutes=30, penalty_type="Fixed Amount", penalty_value=50),
					rule(rule_name="Weekly >45", frequency="Weekly", condition="Greater Than", min_minutes=45, penalty_type="Working Hours", penalty_value=1),
					rule(rule_name="Monthly >100", frequency="Monthly", condition="Greater Than", min_minutes=100, penalty_type="Leave Days", penalty_value=0.5, leave_type=cls.annual),
				],
				"enable_overtime": 1,
				"overtime_basis": "Work After Shift End",
				"overtime_requires_approval": 1,
				"overtime_rules": [
					rule(overtime_category="Working Day", day_type="Working Day", time_of_day="Any", calculation_method="Hourly Rate Multiplier", multiplier=1.35),
					rule(overtime_category="Weekly Off", day_type="Weekly Off", time_of_day="Any", calculation_method="Hourly Rate Multiplier", multiplier=2),
				],
				"holiday_work_compensation": "Monetary and Compensatory Leave",
				"holiday_comp_leave_type": cls.comp,
				"holiday_work_requires_approval": 1,
				"comp_days_weekly_off": 1,
				"comp_days_public_holiday": 1,
				"enable_auto_leave_allocation": 1,
				"leave_entitlement_rules": [
					{"leave_type": cls.annual, "min_service_years": 0, "max_service_years": 1, "annual_entitlement": 0, "allocation_frequency": "Annual", "proration_method": "Prorate by Remaining Months"},
					{"leave_type": cls.annual, "min_service_years": 1, "max_service_years": 2, "annual_entitlement": 15, "allocation_frequency": "Annual", "proration_method": "Prorate by Remaining Months"},
					{"leave_type": cls.annual, "min_service_years": 2, "max_service_years": 0, "annual_entitlement": 21, "allocation_frequency": "Annual", "proration_method": "Prorate by Remaining Months"},
				],
				"leave_policy_rules": [rule(leave_type=cls.annual, min_advance_notice_days=3, allow_during_probation=1)],
				"enable_half_day_rules": 1,
				"half_day_method": "Working Hours Below Minimum",
				"half_day_min_hours": 4,
				"enable_half_day_deduction": 1,
				"half_day_deduction_value": 0.5,
				"half_day_deduction_leave_type": cls.annual,
				"half_day_deduction_trigger": "Half Day Attendance",
				"half_day_deduction_requires_approval": 1,
				"insufficient_balance_action": "Hold for HR Review",
			}
		).insert().name

	def punch(self, d, *times):
		for t in times:
			frappe.get_doc({"doctype": "Employee Checkin", "employee": self.employee, "time": ts(d, t)}).insert()

	def attend(self, d, status="Present", first=None, last=None, hours=None):
		doc = frappe.get_doc(
			{
				"doctype": "Attendance",
				"employee": self.employee,
				"attendance_date": d,
				"status": status,
				"company": self.company,
				"shift": self.shift,
				"in_time": ts(d, first) if first else None,
				"out_time": ts(d, last) if last else None,
				"working_hours": hours,
			}
		)
		doc.insert()
		doc.submit()
		return doc

	def day(self, d, first, last, status="Present"):
		self.punch(d, first, last)
		hours = (ts(d, last) - ts(d, first)).total_seconds() / 3600
		return self.attend(d, status, first, last, hours)

	def allocate(self, leave_type, days, frm="2026-01-01", to="2026-12-31"):
		doc = frappe.get_doc({"doctype": "Leave Allocation", "employee": self.employee, "leave_type": leave_type, "from_date": frm, "to_date": to, "new_leaves_allocated": days})
		doc.insert()
		doc.submit()
		return doc.name

	def penalties(self):
		return frappe.get_all("Attendance Penalty", filters={"employee": self.employee}, fields=["*"], order_by="frequency, period_start")

	# ------------------------------------------------------------------ handbook resolution

	def test_handbook_resolution_priority_and_conflict(self):
		self.assertEqual(get_handbook_name(self.employee, "2026-10-05"), P + "Handbook")
		other = frappe.db.get_value("Employee", {"company": self.company, "name": ["!=", self.employee], "status": "Active"}, "name")
		if other:
			self.assertEqual(get_handbook_name(other, "2026-10-05"), P + "Company Wide")
		self.assertNotEqual(get_handbook_name(self.employee, "2024-12-31"), P + "Handbook")  # before its effective_from
		dup = frappe.get_doc({"doctype": "HR Handbook", "handbook_name": P + "Dup", "company": self.company, "priority": 50, "effective_from": "2026-01-01", "applies_to_all_employees": 1})
		with self.assertRaises(frappe.ValidationError):
			dup.insert()

	# ------------------------------------------------------------------ attendance classification

	def test_half_day_and_wfh_classification(self):
		half = self.day("2026-10-05", "09:20", "12:00")  # 2h40 < 4h
		self.assertEqual(half.status, "Half Day")
		self.assertIn("Half Day", half.policy_note)

		wfh_day = self.day("2026-10-06", "09:00", "17:00")  # Tuesday
		self.assertEqual(wfh_day.status, "Work From Home")

		office = self.day("2026-10-08", "09:00", "17:00")  # Thursday
		self.assertEqual(office.status, "Present")

		absent = self.attend("2026-10-13", "Absent")  # Tuesday, Require Check-in → stays Absent
		self.assertEqual(absent.status, "Absent")

	def test_auto_wfh_is_idempotent_and_respects_leave_and_holidays(self):
		frappe.db.set_value("Shift Type", self.shift, "wfh_attendance_rule", "Mark Automatically")
		frappe.clear_document_cache("Shift Type", self.shift)
		day_facts.clear_cache()
		created = wfh.mark_auto_wfh_attendance(self.employee, "2026-10-12", "2026-10-25")
		self.assertEqual(len(created), 2)  # Tue 13 and Tue 20
		self.assertEqual(frappe.db.get_value("Attendance", created[0], "status"), "Work From Home")
		self.assertEqual(wfh.mark_auto_wfh_attendance(self.employee, "2026-10-12", "2026-10-25"), [])

	# ------------------------------------------------------------------ late penalties

	def test_late_penalties_daily_weekly_monthly_idempotent(self):
		self.day("2026-10-03", "09:30", "17:00")  # Sat: 30 late
		self.day("2026-10-08", "09:25", "17:00")  # Thu: 25 late  → week 3–9 Oct = 55
		self.day("2026-10-12", "09:50", "17:00")  # Mon: 50 late  → month = 105

		start, end = records.lookback_window(AS_OF)
		names = [records.create_penalty(s) for s in records.collect_late_penalties(self.employee, start, end, AS_OF)]
		rows = self.penalties()
		by = {(r.frequency, str(r.period_start)): r for r in rows}

		self.assertEqual(by[("Daily", "2026-10-03")].total_late_minutes, 30)
		self.assertEqual(by[("Daily", "2026-10-08")].total_late_minutes, 25)
		self.assertNotIn(("Daily", "2026-10-12"), by)  # 50 is outside 15–30
		self.assertEqual(by[("Weekly", "2026-10-03")].total_late_minutes, 55)
		monthly = by[("Monthly", "2026-10-01")]
		self.assertEqual(monthly.total_late_minutes, 105)
		# no Annual allocation → the leave-days penalty cannot auto-submit and stays as a draft
		self.assertEqual(monthly.docstatus, 0)
		self.assertEqual(by[("Daily", "2026-10-03")].docstatus, 1)

		# idempotent
		again = [records.create_penalty(s) for s in records.collect_late_penalties(self.employee, start, end, AS_OF)]
		self.assertEqual([n for n in again if n], [])
		self.assertEqual(len(self.penalties()), len([n for n in names if n]))

	def test_handbook_change_does_not_touch_history(self):
		self.day("2026-10-03", "09:30", "17:00")
		start, end = records.lookback_window(AS_OF)
		for s in records.collect_late_penalties(self.employee, start, end, AS_OF):
			records.create_penalty(s)
		hb = frappe.get_doc("HR Handbook", P + "Handbook")
		hb.late_penalty_rules[0].penalty_value = 999
		hb.save()
		clear_request_cache()
		for s in records.collect_late_penalties(self.employee, start, end, AS_OF):
			records.create_penalty(s)
		daily = [r for r in self.penalties() if r.frequency == "Daily"]
		self.assertEqual(len(daily), 1)
		self.assertEqual(flt(daily[0].penalty_value), 50)
		self.assertIn('"penalty_value": 50', daily[0].rule_snapshot)

	def test_cancelled_penalty_is_not_recreated(self):
		self.day("2026-10-03", "09:30", "17:00")
		start, end = records.lookback_window(AS_OF)
		for s in records.collect_late_penalties(self.employee, start, end, AS_OF):
			records.create_penalty(s)
		daily = frappe.get_doc("Attendance Penalty", {"employee": self.employee, "frequency": "Daily"})
		daily.cancel()
		for s in records.collect_late_penalties(self.employee, start, end, AS_OF):
			records.create_penalty(s)
		self.assertEqual(frappe.db.count("Attendance Penalty", {"employee": self.employee, "frequency": "Daily"}), 1)

	def test_approved_permission_is_not_late_time(self):
		perm = frappe.get_doc(
			{
				"doctype": "Employee Permission",
				"employee": self.employee,
				"permission_type": "Late Arrival",
				"repetition_type": "One-time",
				"from_date": "2026-10-08",
				"start_time": "09:00:00",
				"end_time": "09:30:00",
				"reason": "Doctor",
				"status": "Approved",
			}
		).insert()
		perm.submit()
		self.day("2026-10-08", "09:25", "17:00")
		f = day_facts.get_single_day(self.employee, "2026-10-08")
		self.assertEqual((f.raw_late_minutes, f.late_permission_minutes), (25, 25))
		start, end = records.lookback_window(AS_OF)
		specs = records.collect_late_penalties(self.employee, start, end, AS_OF)
		self.assertFalse([s for s in specs if s.frequency == "Daily"])

	def test_permission_overlap_and_limits(self):
		base = {"doctype": "Employee Permission", "employee": self.employee, "permission_type": "Temporary Absence", "reason": "x", "status": "Open"}
		frappe.get_doc({**base, "repetition_type": "Weekly", "from_date": "2026-10-01", "to_date": "2026-10-31", "on_monday": 1, "start_time": "12:00:00", "end_time": "13:00:00"}).insert()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({**base, "repetition_type": "One-time", "from_date": "2026-10-12", "start_time": "12:30:00", "end_time": "14:00:00"}).insert()
		frappe.get_doc({**base, "repetition_type": "One-time", "from_date": "2026-10-13", "start_time": "12:30:00", "end_time": "14:00:00"}).insert()

	# ------------------------------------------------------------------ overtime

	def test_holiday_overtime_comp_leave_and_reversal(self):
		self.punch("2026-10-09", "10:00", "16:00")  # Friday (weekly off), no attendance needed
		self.day("2026-10-15", "09:00", "19:00")  # working day, 2h after shift
		created = records.create_overtime(self.employee, "2026-10-01", "2026-10-31", AS_OF)
		self.assertEqual(len(created), 2)
		self.assertEqual(records.create_overtime(self.employee, "2026-10-01", "2026-10-31", AS_OF), [])

		holiday = frappe.get_doc("Employee Overtime", {"employee": self.employee, "attendance_date": "2026-10-09"})
		self.assertEqual((holiday.day_type, holiday.overtime_minutes, holiday.payable_hours, holiday.comp_leave_days), ("Weekly Off", 360, 12, 1))
		self.assertEqual(holiday.docstatus, 0)  # requires approval
		working = frappe.get_doc("Employee Overtime", {"employee": self.employee, "attendance_date": "2026-10-15"})
		self.assertEqual((working.overtime_minutes, working.payable_hours), (120, 2.7))

		holiday.submit()  # approval → compensatory leave granted through a Leave Allocation
		self.assertTrue(holiday.leave_allocation)
		self.assertEqual(leave_adjustment.get_balance(self.employee, self.comp, "2026-10-20"), 1)
		holiday.cancel()
		self.assertEqual(leave_adjustment.get_balance(self.employee, self.comp, "2026-10-20"), 0)

	# ------------------------------------------------------------------ leave

	def test_entitlement_allocation_milestone_and_idempotency(self):
		res = leave_entitlement.sync_employee(self.employee, "2026-03-01")  # 1 completed year
		self.assertEqual(res[0]["result"], "created")
		alloc = res[0]["allocation"]
		self.assertEqual(flt(frappe.db.get_value("Leave Allocation", alloc, "new_leaves_allocated")), 15)
		self.assertEqual(leave_entitlement.sync_employee(self.employee, "2026-03-01")[0]["result"], "up to date")

		res = leave_entitlement.sync_employee(self.employee, "2026-07-01")  # 2 years reached on 1 June
		expected = leave_entitlement.apply_rounding(self.annual, (5 * 15 + 7 * 21) / 12)
		self.assertEqual(flt(frappe.db.get_value("Leave Allocation", alloc, "new_leaves_allocated")), expected)
		self.assertEqual(frappe.db.count("Leave Allocation", {"employee": self.employee, "leave_type": self.annual, "docstatus": 1}), 1)
		self.assertEqual(frappe.db.get_value("Leave Allocation", alloc, "hr_handbook"), P + "Handbook")

	def test_manual_allocation_is_never_modified(self):
		manual = self.allocate(self.annual, 5)
		res = leave_entitlement.sync_employee(self.employee, "2026-07-01")
		self.assertIn("manual", res[0]["result"])
		self.assertEqual(flt(frappe.db.get_value("Leave Allocation", manual, "new_leaves_allocated")), 5)

	def test_leave_policy_advance_notice_and_override(self):
		doc = frappe.get_doc(
			{"doctype": "Leave Application", "employee": self.employee, "leave_type": self.annual, "from_date": "2026-10-20", "to_date": "2026-10-20",
			 "posting_date": "2026-10-19", "total_leave_days": 1, "status": "Open"}
		)
		with self.assertRaises(leave_policy.HandbookLeavePolicyError):
			leave_policy.validate_leave_application(doc)
		doc.posting_date = "2026-10-15"
		leave_policy.validate_leave_application(doc)  # 5 days notice → ok
		doc.posting_date = "2026-10-19"
		doc.override_handbook_policy = 1
		leave_policy.validate_leave_application(doc)  # Administrator is HR Manager → bypass allowed

	def test_casual_leave_also_deducted_from_annual(self):
		casual = self.leave_type(P + "Casual")
		hb = frappe.get_doc("HR Handbook", P + "Handbook")
		hb.append("leave_policy_rules", {"enabled": 1, "leave_type": casual, "deduct_from_leave_type": self.annual, "allow_during_probation": 1})
		hb.save()
		clear_request_cache()
		self.allocate(self.annual, 10)
		self.allocate(casual, 6)
		app = frappe.get_doc(
			{"doctype": "Leave Application", "employee": self.employee, "leave_type": casual, "from_date": "2026-10-19", "to_date": "2026-10-20",
			 "posting_date": "2026-10-18", "status": "Approved", "leave_approver": "Administrator"}
		)
		app.insert()
		app.submit()
		self.assertEqual(app.handbook_deducted_days, 2)
		self.assertEqual(leave_adjustment.get_balance(self.employee, self.annual, "2026-10-25"), 8)
		self.assertEqual(leave_adjustment.get_balance(self.employee, casual, "2026-10-25"), 4)
		app.cancel()
		self.assertEqual(leave_adjustment.get_balance(self.employee, self.annual, "2026-10-25"), 10)

	def test_half_day_deduction_apply_and_reverse(self):
		self.allocate(self.annual, 10)
		self.day("2026-10-05", "09:00", "11:00")  # Half Day
		created = records.create_half_day_deductions(self.employee, "2026-10-01", "2026-10-31")
		self.assertEqual(len(created), 1)
		self.assertEqual(records.create_half_day_deductions(self.employee, "2026-10-01", "2026-10-31"), [])
		ded = frappe.get_doc("Half Day Leave Deduction", created[0])
		self.assertEqual(ded.docstatus, 0)  # requires approval
		ded.submit()
		self.assertEqual((ded.deducted_days, ded.deduction_status), (0.5, "Applied"))
		self.assertEqual(leave_adjustment.get_balance(self.employee, self.annual, "2026-10-20"), 9.5)
		ded.cancel()
		self.assertEqual(leave_adjustment.get_balance(self.employee, self.annual, "2026-10-20"), 10)

	def test_half_day_deduction_insufficient_balance(self):
		self.day("2026-10-05", "09:00", "11:00")
		name = records.create_half_day_deductions(self.employee, "2026-10-01", "2026-10-31")[0]
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Half Day Leave Deduction", name).submit()

	# ------------------------------------------------------------------ shifts & days

	def test_shift_change_and_missing_checkout(self):
		frappe.get_doc({"doctype": "Shift Assignment", "employee": self.employee, "shift_type": self.shift2, "start_date": "2026-10-19", "company": self.company, "status": "Active"}).submit()
		self.punch("2026-10-19", "10:20")
		f18, f19 = day_facts.get_day_facts(self.employee, "2026-10-18", "2026-10-19")
		self.assertEqual((f18.shift, f19.shift), (self.shift, self.shift2))
		self.assertEqual(f19.raw_late_minutes, 20)  # relative to the new 10:00 start
		self.assertTrue(f19.missing_check_out)

	# ------------------------------------------------------------------ dashboard & API

	def link_user(self):
		user = frappe.get_doc({"doctype": "User", "email": "_t_hrp_emp@example.com", "first_name": "T", "send_welcome_email": 0, "roles": [{"role": "Employee"}]}).insert(ignore_permissions=True)
		frappe.db.set_value("Employee", self.employee, "user_id", user.name)
		clear_request_cache()
		return user.name

	def test_dashboard_summary(self):
		self.day("2026-10-03", "09:30", "17:00")  # Present, 30 late
		self.day("2026-10-05", "09:00", "11:00")  # Half Day
		self.day("2026-10-06", "09:00", "17:00")  # WFH
		self.attend("2026-10-13", "Absent")
		res = api.get_dashboard(year=2026, month=10, employee=self.employee)
		s = res["data"]["summary"]
		self.assertEqual(res["period"]["start"], "2026-10-01")
		self.assertEqual(s["total_working_days"], 31 - 5 - 1)  # minus Fridays and the public holiday
		self.assertEqual((s["absent_days"], s["half_days"], s["work_from_home_days"]), (1, 1, 1))
		self.assertEqual(s["actual_worked_days"], 2.5)
		self.assertEqual(s["total_late_minutes"], 30)
		days = {d["date"]: d for d in res["data"]["days"]}
		self.assertEqual(days["2026-10-09"]["status"], "Weekly Off")
		self.assertEqual(days["2026-10-06"]["work_mode"], "Work From Home")
		self.assertEqual(days["2026-10-03"]["late_minutes"], 30)

	def test_custom_attendance_period(self):
		frappe.db.set_value("HR Handbook", P + "Handbook", {"attendance_period_type": "Custom Period", "period_start_day": 26})
		frappe.clear_document_cache("HR Handbook", P + "Handbook")
		clear_request_cache()
		res = api.get_summary(year=2026, month=11, employee=self.employee)
		self.assertEqual((res["period"]["start"], res["period"]["end"]), ("2026-10-26", "2026-11-25"))

	def test_api_authorization(self):
		user = self.link_user()
		frappe.set_user(user)
		self.assertEqual(api.get_summary(year=2026, month=10)["employee"]["name"], self.employee)
		other = frappe.db.get_value("Employee", {"name": ["!=", self.employee]}, "name")
		if other:
			with self.assertRaises(frappe.PermissionError):
				api.get_summary(year=2026, month=10, employee=other)
		with self.assertRaises(frappe.ValidationError):
			api.get_summary(year=2026, month=13)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.get_summary()
		frappe.set_user("Administrator")  # HR may view others
		self.assertEqual(api.get_summary(year=2026, month=10, employee=self.employee)["employee"]["name"], self.employee)

	def test_daily_job_runs_end_to_end(self):
		self.allocate(self.comp, 0.0001)  # ensure ledger paths work with an existing allocation
		self.day("2026-10-03", "09:30", "17:00")
		self.punch("2026-10-09", "10:00", "16:00")
		first = jobs.process_employee(self.employee, AS_OF)
		self.assertFalse([k for k, v in first.items() if isinstance(v, str) and v.startswith("error")], first)
		second = jobs.process_employee(self.employee, AS_OF)
		self.assertEqual(second["late_penalties"], [])
		self.assertEqual(second["overtime"], [])

	# ------------------------------------------------------------------ forgotten check-in requests

	def request(self, date, time, **kw):
		return frappe.get_doc({"doctype": "Checkin Request", "employee": self.employee, "date": date, "time": time, "reason": "forgot", **kw}).insert()

	def test_approval_creates_checkin_and_resets_attendance(self):
		self.punch("2026-10-15", "09:02")  # forgot to check out
		att = self.attend("2026-10-15", "Half Day", "09:02", None, 0)
		req = self.request("2026-10-15", "17:35:00")
		req.status = "Approved"
		req.save()
		req.submit()
		self.assertTrue(frappe.db.exists("Employee Checkin", req.employee_checkin))
		self.assertEqual(frappe.db.get_value("Employee Checkin", req.employee_checkin, "time"), ts("2026-10-15", "17:35"))
		self.assertEqual(req.attendance_reset, att.name)
		self.assertEqual(frappe.db.get_value("Attendance", att.name, "docstatus"), 2)
		f = day_facts.get_single_day(self.employee, "2026-10-15")
		self.assertEqual(f.worked_minutes, 8 * 60 + 33)
		req.cancel()
		self.assertFalse(frappe.db.exists("Employee Checkin", req.employee_checkin))

	def test_rejected_request_changes_nothing(self):
		req = self.request("2026-10-15", "17:35:00")
		req.status = "Rejected"
		req.save()
		req.submit()
		self.assertFalse(req.employee_checkin)
		self.assertEqual(frappe.db.count("Employee Checkin", {"employee": self.employee}), 0)

	def test_validations(self):
		with self.assertRaises(frappe.ValidationError):
			self.request("2099-01-01", "09:00:00")  # future
		self.punch("2026-10-15", "09:00")
		with self.assertRaises(frappe.ValidationError):
			self.request("2026-10-15", "09:00:30")  # a punch already exists then
		self.request("2026-10-15", "17:30:00")
		with self.assertRaises(frappe.ValidationError):
			self.request("2026-10-15", "17:30:20")  # duplicate request
		req = self.request("2026-10-16", "17:30:00")
		with self.assertRaises(frappe.ValidationError):
			req.submit()  # still Open

	def test_employee_cannot_approve_own_request(self):
		user = self.link_user()
		req = self.request("2026-10-15", "17:35:00")
		frappe.set_user(user)
		req.status = "Approved"
		with self.assertRaises(frappe.ValidationError):
			req.validate_status_change()

	def test_monthly_limit_and_api(self):
		frappe.db.set_value("HR Handbook", P + "Handbook", "max_checkin_requests_per_month", 1)
		frappe.clear_document_cache("HR Handbook", P + "Handbook")
		clear_request_cache()
		user = self.link_user()
		frappe.set_user(user)
		res = api.create_checkin_request(date="2026-10-15", time="17:35", reason="forgot")
		self.assertTrue(res["data"]["name"])
		self.assertEqual(api.get_checkin_requests()["data"]["total"], 1)
		with self.assertRaises(frappe.ValidationError):
			api.create_checkin_request(date="2026-10-20", time="17:35", reason="again")
		with self.assertRaises(frappe.ValidationError):
			api.create_checkin_request(date="2026-10-21", time="17:35", reason=" ")

	# ------------------------------------------------------------------ employee portal self-service

	def test_portal_leave_permission_and_withdraw(self):
		self.allocate(self.annual, 10)
		user = self.link_user()
		frappe.set_user(user)
		frappe.flags.current_date = None

		opts = api.get_request_options()["data"]
		annual = next(lt for lt in opts["leave_types"] if lt["leave_type"] == self.annual)
		self.assertIn("3 days' notice", annual["hints"])

		with self.assertRaises(frappe.ValidationError):  # handbook: 3 days' notice
			api.create_leave_application(leave_type=self.annual, from_date=frappe.utils.today(), to_date=frappe.utils.today(), reason="x")
		start = frappe.utils.add_days(frappe.utils.today(), 10)
		while frappe.utils.getdate(start).weekday() == 4:  # avoid Friday (weekly off) in the test holiday list
			start = frappe.utils.add_days(start, 1)
		res = api.create_leave_application(leave_type=self.annual, from_date=start, to_date=start, reason="family")["data"]
		self.assertEqual(frappe.db.get_value("Leave Application", res["name"], ["status", "employee"]), ("Open", self.employee))

		perm = api.create_permission_request(permission_type="Late Arrival", date=start, start_time="09:00", end_time="10:00", reason="doctor")["data"]
		self.assertEqual(perm["minutes"], 60)
		with self.assertRaises(frappe.ValidationError):
			api.create_permission_request(permission_type="Bogus", date=start, start_time="09:00", end_time="10:00", reason="x")

		rows = api.get_my_requests()["data"]
		self.assertEqual({r["kind"] for r in rows}, {"Leave", "Permission"})
		self.assertTrue(all(r["state"] == "Pending Approval" and r["can_withdraw"] for r in rows))

		api.withdraw_request(doctype="Employee Permission", name=perm["name"])
		self.assertFalse(frappe.db.exists("Employee Permission", perm["name"]))
		with self.assertRaises(frappe.ValidationError):
			api.withdraw_request(doctype="Salary Slip", name="X")

	def test_portal_cannot_withdraw_someone_elses_request(self):
		other = frappe.db.get_value("Employee", {"name": ["!=", self.employee], "status": "Active"}, "name")
		if not other:
			return
		perm = frappe.get_doc({"doctype": "Employee Permission", "employee": other, "permission_type": "Late Arrival", "repetition_type": "One-time",
			"from_date": "2026-10-12", "start_time": "09:00:00", "end_time": "09:30:00", "reason": "x", "status": "Open"}).insert()
		frappe.set_user(self.link_user())
		with self.assertRaises(frappe.PermissionError):
			api.withdraw_request(doctype="Employee Permission", name=perm.name)

	def test_bulk_checkin_tool(self):
		from mobility_hr.mobility_hr.doctype.bulk_checkin_tool.bulk_checkin_tool import run

		self.punch("2026-10-20", "09:00")  # this employee already has the 09:00 punch → skipped, others continue
		other = frappe.get_doc({"doctype": "Employee", "first_name": P + "Second", "gender": frappe.db.get_value("Gender", {}, "name"),
			"date_of_birth": "1990-01-01", "date_of_joining": "2024-06-01", "company": self.company, "status": "Active"}).insert()
		res = run([self.employee, other.name], "2026-10-20", "09:00:00", "IN", "Device offline")
		self.assertEqual((res["created"], res["failed"]), (1, 1))
		ok = next(r for r in res["results"] if r["ok"])
		self.assertEqual(ok["employee"], other.name)
		req = frappe.get_doc("Checkin Request", ok["request"])
		self.assertEqual((req.docstatus, req.status, req.source), (1, "Approved", "HR Bulk Entry"))
		self.assertEqual(frappe.db.get_value("Employee Checkin", req.employee_checkin, ["employee", "log_type"]), (other.name, "IN"))
		bad = next(r for r in res["results"] if not r["ok"])
		self.assertIn("already exists", bad["error"])
