"""Pure business-logic tests (no database writes)."""

import unittest
from datetime import date, datetime, time

import frappe

from mobility_hr.hr_policy import half_day, lateness, leave_entitlement, overtime, periods, permissions
from mobility_hr.hr_policy.day_facts import DayFacts, build_intervals

D = frappe._dict


def dt(s):
	return datetime.strptime(s, "%Y-%m-%d %H:%M")


def log(t, log_type=None):
	return D(time=dt(t), log_type=log_type)


class TestPeriods(unittest.TestCase):
	def test_calendar_month(self):
		p = periods.get_period(2028, 2, 1)
		self.assertEqual((p.start, p.end), (date(2028, 2, 1), date(2028, 2, 29)))

	def test_custom_period_crosses_year(self):
		p = periods.get_period(2026, 1, 26)
		self.assertEqual((p.start, p.end), (date(2025, 12, 26), date(2026, 1, 25)))

	def test_period_for_date(self):
		self.assertEqual(periods.get_period_for_date(date(2026, 12, 27), 26).label, "2027-01")
		self.assertEqual(periods.get_period_for_date(date(2026, 12, 25), 26).label, "2026-12")
		self.assertEqual(periods.get_period_for_date(date(2026, 12, 26), 26).start, date(2026, 12, 26))

	def test_periods_are_contiguous(self):
		prev = periods.get_period(2026, 1, 26)
		for m in range(2, 13):
			cur = periods.get_period(2026, m, 26)
			self.assertEqual((cur.start - prev.end).days, 1)
			prev = cur

	def test_invalid_start_day(self):
		with self.assertRaises(ValueError):
			periods.get_period(2026, 1, 29)

	def test_week(self):
		self.assertEqual(periods.get_week(date(2026, 11, 5), "Saturday"), (date(2026, 10, 31), date(2026, 11, 6)))
		self.assertEqual(periods.get_week(date(2026, 11, 5), "Monday"), (date(2026, 11, 2), date(2026, 11, 8)))


class TestLateRules(unittest.TestCase):
	rules = [
		D(enabled=1, rule_name="A", frequency="Daily", condition="Between", min_minutes=15, max_minutes=30, penalty_type="Fixed Amount", penalty_value=50),
		D(enabled=1, rule_name="B", frequency="Daily", condition="Greater Than", min_minutes=60, penalty_type="Leave Days", penalty_value=0.5),
		D(enabled=1, rule_name="W", frequency="Weekly", condition="Greater Than", min_minutes=45, penalty_type="Working Hours", penalty_value=1),
		D(enabled=0, rule_name="X", frequency="Daily", condition="Greater Than", min_minutes=0, penalty_type="Warning Only"),
	]

	def match(self, minutes, freq="Daily"):
		r = lateness.match_rule(self.rules, freq, minutes)
		return r.rule_name if r else None

	def test_between_boundaries_inclusive(self):
		self.assertIsNone(self.match(14))
		self.assertEqual(self.match(15), "A")
		self.assertEqual(self.match(30), "A")
		self.assertIsNone(self.match(31))

	def test_greater_than_is_exclusive(self):
		self.assertIsNone(self.match(60))
		self.assertEqual(self.match(61), "B")

	def test_disabled_and_frequency(self):
		self.assertIsNone(self.match(5))  # only the disabled rule would match
		self.assertEqual(self.match(46, "Weekly"), "W")
		self.assertIsNone(self.match(46, "Monthly"))

	def test_highest_tier_wins(self):
		rules = [*self.rules, D(enabled=1, rule_name="C", frequency="Daily", condition="Greater Than", min_minutes=20, penalty_type="Fixed Amount", penalty_value=80)]
		self.assertEqual(lateness.match_rule(rules, "Daily", 25).rule_name, "C")

	def test_grace_handling(self):
		ignore = "Ignore lateness within grace, count full minutes beyond"
		self.assertEqual(lateness.counted_late(10, 0, 10, ignore), 0)
		self.assertEqual(lateness.counted_late(11, 0, 10, ignore), 11)
		self.assertEqual(lateness.counted_late(25, 0, 10, lateness.SUBTRACT_GRACE), 15)

	def test_permission_removes_late_minutes(self):
		self.assertEqual(lateness.counted_late(40, 30, 0, None), 10)
		self.assertEqual(lateness.counted_late(20, 30, 0, None), 0)


class TestIntervals(unittest.TestCase):
	def test_multiple_intervals_not_first_minus_last(self):
		logs = [log("2026-10-05 09:00"), log("2026-10-05 12:00"), log("2026-10-05 13:00"), log("2026-10-05 17:00")]
		intervals, miss_in, miss_out = build_intervals(logs, None)
		self.assertEqual(len(intervals), 2)
		self.assertEqual(sum((b - a).seconds for a, b in intervals) // 60, 7 * 60)  # not 8h
		self.assertFalse(miss_in or miss_out)

	def test_missing_checkout_and_duplicates(self):
		logs = [log("2026-10-05 09:00"), log("2026-10-05 09:00"), log("2026-10-05 09:00")]
		intervals, _mi, miss_out = build_intervals(logs, None)
		self.assertEqual(intervals, [])
		self.assertTrue(miss_out)

	def test_strict_mode_missing_checkin(self):
		logs = [log("2026-10-05 08:00", "OUT"), log("2026-10-05 09:00", "IN"), log("2026-10-05 17:00", "OUT")]
		intervals, miss_in, miss_out = build_intervals(logs, "Strictly based on Log Type in Employee Checkin")
		self.assertEqual(len(intervals), 1)
		self.assertTrue(miss_in)
		self.assertFalse(miss_out)


def facts(intervals, shift=("09:00", "17:00"), holiday=None, d=date(2026, 10, 5)):
	s = datetime.combine(d, time.fromisoformat(shift[0]))
	e = datetime.combine(d, time.fromisoformat(shift[1]))
	if e <= s:
		e = e.replace(day=e.day + 1)
	ivals = [(dt(a), dt(b)) for a, b in intervals]
	return DayFacts(
		employee="E",
		date=d,
		weekday="Monday",
		shift="S",
		shift_start=s,
		shift_end=e,
		standard_minutes=int((e - s).total_seconds() // 60),
		holiday_type=holiday,
		intervals=ivals,
		worked_minutes=int(sum((b - a).total_seconds() for a, b in ivals) // 60),
	)


def handbook(**kw):
	base = D(
		name="HB",
		enable_overtime=1,
		overtime_basis="Work After Shift End",
		count_early_arrival=0,
		night_start=None,
		night_end=None,
		holiday_work_compensation="None",
		holiday_comp_leave_type="Comp Off",
		comp_days_weekly_off=1,
		comp_days_public_holiday=2,
		min_hours_for_full_comp_day=0,
		overtime_rules=[],
	)
	base.update(kw)
	return base


def ot_rule(**kw):
	r = D(enabled=1, overtime_category="OT", day_type="Working Day", time_of_day="Any", min_overtime_minutes=0, min_eligible_minutes=0,
		  calculation_method="Hourly Rate Multiplier", multiplier=1.35, fixed_rate=0, rounding_method="No Rounding", max_hours_per_day=0,
		  overtime_type=None, is_statutory=0)
	r.update(kw)
	return r


class TestOvertime(unittest.TestCase):
	def test_after_shift_end_with_break(self):
		f = facts([("2026-10-05 08:50", "2026-10-05 13:00"), ("2026-10-05 14:00", "2026-10-05 19:30")])
		spec = overtime.calculate(f, handbook(overtime_rules=[ot_rule(rounding_method="Down to 15 Minutes")]))
		self.assertEqual(spec.counted_minutes, 150)
		self.assertAlmostEqual(spec.payable_hours, 2.5 * 1.35)

	def test_exceeding_shift_hours_basis(self):
		f = facts([("2026-10-05 08:50", "2026-10-05 13:00"), ("2026-10-05 14:00", "2026-10-05 19:30")])
		spec = overtime.calculate(f, handbook(overtime_basis="Worked Hours Exceeding Shift Hours", overtime_rules=[ot_rule()]))
		self.assertEqual(spec.counted_minutes, 580 - 480)

	def test_normal_hours_never_overtime(self):
		f = facts([("2026-10-05 09:00", "2026-10-05 17:00")])
		self.assertIsNone(overtime.calculate(f, handbook(overtime_rules=[ot_rule()])))

	def test_buffer_rounding_and_minimum(self):
		f = facts([("2026-10-05 09:00", "2026-10-05 18:10")])  # 70 min after shift
		hb = handbook(overtime_rules=[ot_rule(min_overtime_minutes=15, rounding_method="Down to 30 Minutes")])
		self.assertEqual(overtime.calculate(f, hb).counted_minutes, 30)  # 55 → 30
		hb = handbook(overtime_rules=[ot_rule(min_eligible_minutes=90)])
		self.assertIsNone(overtime.calculate(f, hb))

	def test_day_night_split(self):
		f = facts([("2026-10-05 09:00", "2026-10-05 21:00")])
		hb = handbook(
			night_start="19:00:00",
			night_end="06:00:00",
			overtime_rules=[ot_rule(time_of_day="Day", multiplier=1.35), ot_rule(overtime_category="Night", time_of_day="Night", multiplier=1.7)],
		)
		spec = overtime.calculate(f, hb)
		by_tod = {line.time_of_day: line.counted_minutes for line in spec.lines}
		self.assertEqual(by_tod, {"Day": 120, "Night": 120})
		self.assertAlmostEqual(spec.payable_hours, 2 * 1.35 + 2 * 1.7)

	def test_overnight_shift(self):
		f = facts([("2026-10-05 22:00", "2026-10-06 07:00")], shift=("22:00", "06:00"))
		spec = overtime.calculate(f, handbook(overtime_rules=[ot_rule()]))
		self.assertEqual(spec.counted_minutes, 60)

	def test_holiday_work_monetary_and_comp_leave(self):
		f = facts([("2026-10-09 10:00", "2026-10-09 13:00")], holiday="Weekly Off", d=date(2026, 10, 9))
		hb = handbook(
			holiday_work_compensation="Monetary and Compensatory Leave",
			min_hours_for_full_comp_day=4,
			overtime_rules=[ot_rule(day_type="Weekly Off", multiplier=2)],
		)
		spec = overtime.calculate(f, hb)
		self.assertEqual(spec.counted_minutes, 180)  # every worked minute
		self.assertEqual(spec.payable_hours, 6)
		self.assertEqual(spec.comp_leave_days, 0.5)  # < 4 h → half
		self.assertEqual(spec.compensation, "Monetary and Compensatory Leave")

	def test_public_holiday_comp_only(self):
		f = facts([("2026-10-06 09:00", "2026-10-06 17:00")], holiday="Public Holiday", d=date(2026, 10, 6))
		spec = overtime.calculate(f, handbook(enable_overtime=0, holiday_work_compensation="Compensatory Leave"))
		self.assertEqual((spec.compensation, spec.comp_leave_days, spec.counted_minutes), ("Compensatory Leave", 2, 0))


class TestHalfDay(unittest.TestCase):
	hb = D(half_day_method="Any Condition", half_day_min_hours=4, half_day_min_percentage=0, half_day_leave_before="13:00:00")

	def test_hours(self):
		half, _ = half_day.evaluate(self.hb, 180, 480, dt("2026-10-05 15:00"), 0, False, "2026-10-05")
		self.assertTrue(half)

	def test_permission_counts_as_worked(self):
		half, _ = half_day.evaluate(self.hb, 180, 480, dt("2026-10-05 15:00"), 60, False, "2026-10-05")
		self.assertFalse(half)

	def test_left_early_unless_permitted(self):
		self.assertTrue(half_day.evaluate(self.hb, 300, 480, dt("2026-10-05 12:00"), 0, False, "2026-10-05")[0])
		self.assertFalse(half_day.evaluate(self.hb, 300, 480, dt("2026-10-05 12:00"), 0, True, "2026-10-05")[0])

	def test_percentage(self):
		hb = D(half_day_method="Attendance Percentage Below Minimum", half_day_min_percentage=60)
		self.assertTrue(half_day.evaluate(hb, 240, 480, None, 0, False, "2026-10-05")[0])
		self.assertFalse(half_day.evaluate(hb, 300, 480, None, 0, False, "2026-10-05")[0])


ENTITLEMENT = [
	D(leave_type="AL", min_service_years=0, max_service_years=1, annual_entitlement=0, allocation_frequency="Annual", proration_method="Prorate by Remaining Months"),
	D(leave_type="AL", min_service_years=1, max_service_years=2, annual_entitlement=15, allocation_frequency="Annual", proration_method="Prorate by Remaining Months"),
	D(leave_type="AL", min_service_years=2, max_service_years=10, annual_entitlement=21, allocation_frequency="Annual", proration_method="Prorate by Remaining Months"),
	D(leave_type="AL", min_service_years=10, max_service_years=0, annual_entitlement=30, allocation_frequency="Annual", proration_method="Prorate by Remaining Months"),
]
YEAR_2026 = leave_entitlement.LeavePeriodRange(date(2026, 1, 1), date(2026, 12, 31))


class TestEntitlement(unittest.TestCase):
	def target(self, doj, as_of, rules=ENTITLEMENT):
		return leave_entitlement.calculate_target(rules, "AL", date.fromisoformat(doj), YEAR_2026, date.fromisoformat(as_of))[0]

	def test_service_years(self):
		self.assertEqual(leave_entitlement.completed_years(date(2024, 6, 1), date(2026, 5, 31)), 1)
		self.assertEqual(leave_entitlement.completed_years(date(2024, 6, 1), date(2026, 6, 1)), 2)

	def test_rule_selection(self):
		self.assertEqual(leave_entitlement.rule_for(ENTITLEMENT, "AL", 0).annual_entitlement, 0)
		self.assertEqual(leave_entitlement.rule_for(ENTITLEMENT, "AL", 9).annual_entitlement, 21)
		self.assertEqual(leave_entitlement.rule_for(ENTITLEMENT, "AL", 10).annual_entitlement, 30)

	def test_less_than_one_year(self):
		self.assertEqual(self.target("2025-12-01", "2026-03-01"), 0)

	def test_milestone_mid_year(self):
		self.assertAlmostEqual(self.target("2024-06-01", "2026-03-01"), 15)  # before milestone
		self.assertAlmostEqual(self.target("2024-06-01", "2026-07-01"), (5 * 15 + 7 * 21) / 12)  # after: top-up

	def test_join_mid_year_prorated(self):
		rules = [D(leave_type="AL", min_service_years=0, max_service_years=0, annual_entitlement=15, allocation_frequency="Annual", proration_method="Prorate by Remaining Months")]
		self.assertAlmostEqual(self.target("2026-04-15", "2026-05-01", rules), 15 * 8 / 12)  # May–Dec

	def test_monthly_frequency(self):
		rules = [D(leave_type="AL", min_service_years=0, max_service_years=0, annual_entitlement=12, allocation_frequency="Monthly", proration_method="Prorate by Remaining Months")]
		self.assertAlmostEqual(self.target("2020-01-01", "2026-03-10", rules), 3)


class TestPermissions(unittest.TestCase):
	def p(self, **kw):
		base = D(name="P", repetition_type="One-time", from_date="2026-10-05", to_date=None, start_time="09:00:00", end_time="10:00:00")
		base.update(kw)
		return base

	def test_one_time(self):
		p = self.p()
		self.assertTrue(permissions.occurs_on(p, date(2026, 10, 5)))
		self.assertFalse(permissions.occurs_on(p, date(2026, 10, 6)))

	def test_weekly_and_monthly(self):
		weekly = self.p(repetition_type="Weekly", to_date="2026-12-31", on_monday=1)
		self.assertTrue(permissions.occurs_on(weekly, date(2026, 10, 12)))
		self.assertFalse(permissions.occurs_on(weekly, date(2026, 10, 13)))
		monthly = self.p(repetition_type="Monthly", to_date="2026-12-31")
		self.assertTrue(permissions.occurs_on(monthly, date(2026, 11, 5)))
		self.assertFalse(permissions.occurs_on(monthly, date(2026, 11, 6)))

	def test_overlap(self):
		weekly = self.p(name="W", repetition_type="Weekly", to_date="2026-12-31", on_monday=1)
		clash = self.p(name="O", from_date="2026-10-12", start_time="09:30:00", end_time="10:30:00")
		self.assertEqual(permissions.find_overlap(clash, [weekly])[0], "W")
		no_clash = self.p(name="O", from_date="2026-10-12", start_time="10:00:00", end_time="11:00:00")
		self.assertIsNone(permissions.find_overlap(no_clash, [weekly]))
		other_day = self.p(name="O", from_date="2026-10-13")
		self.assertIsNone(permissions.find_overlap(other_day, [weekly]))


class TestHandbookAdditions(unittest.TestCase):
	"""Rules added for the Mobility Pro Egypt handbook (Jan 2023)."""

	def day(self, d, raw, counted, permission=0):
		return lateness.LateDay(date=date(2026, 10, d), attendance=None, shift=None, shift_start=None, first_in=None, raw=raw, permission=permission, grace=10, counted=counted)

	def test_monthly_grace_cap(self):
		# 6 days 8 min late (forgiven by the 10-min grace) = 48 forgiven; cap 40 → 8 added back
		days = [self.day(i, 8, 0) for i in range(1, 7)] + [self.day(8, 40, 40)]
		self.assertEqual(lateness.period_total(days, 40), (48, 8))
		self.assertEqual(lateness.period_total(days, 0), (40, 0))

	def test_handbook_tardiness_tiers(self):
		rules = [
			D(enabled=1, rule_name="45-60", frequency="Monthly", condition="Between", min_minutes=45, max_minutes=60, penalty_type="Day's Salary Fraction", penalty_value=0.5),
			D(enabled=1, rule_name="61-75", frequency="Monthly", condition="Between", min_minutes=61, max_minutes=75, penalty_type="Day's Salary Fraction", penalty_value=1),
			D(enabled=1, rule_name="76-90", frequency="Monthly", condition="Between", min_minutes=76, max_minutes=90, penalty_type="Day's Salary Fraction", penalty_value=2),
			D(enabled=1, rule_name="91-120", frequency="Monthly", condition="Between", min_minutes=91, max_minutes=120, penalty_type="Day's Salary Fraction", penalty_value=3),
			D(enabled=1, rule_name="121+", frequency="Monthly", condition="Greater Than", min_minutes=120, penalty_type="Day's Salary Fraction", penalty_value=5),
		]
		expect = {44: None, 45: 0.5, 60: 0.5, 61: 1, 75: 1, 76: 2, 90: 2, 91: 3, 120: 3, 121: 5, 300: 5}
		for minutes, value in expect.items():
			r = lateness.match_rule(rules, "Monthly", minutes)
			self.assertEqual(r.penalty_value if r else None, value, minutes)

	def test_arrive_after_and_leave_before(self):
		hb = D(half_day_method="Any Condition", half_day_leave_before="16:00:00", half_day_arrive_after="11:00:00")
		ev = lambda first, last, late_ok=False, early_ok=False: half_day.evaluate(  # noqa: E731
			hb, 300, 480, dt(f"2026-10-05 {last}"), 0, early_ok, "2026-10-05", first_in=dt(f"2026-10-05 {first}"), late_permission_covers=late_ok
		)[0]
		self.assertFalse(ev("09:05", "17:30"))
		self.assertTrue(ev("11:01", "17:30"))
		self.assertFalse(ev("11:00", "17:30"))
		self.assertFalse(ev("11:30", "17:30", late_ok=True))
		self.assertTrue(ev("09:00", "15:59"))
		self.assertFalse(ev("09:00", "15:59", early_ok=True))

	def test_six_month_service_rule(self):
		rules = [D(leave_type="Sick", min_service_years=0.5, max_service_years=0, annual_entitlement=5, allocation_frequency="Annual", proration_method="None")]
		self.assertEqual(leave_entitlement.service_years(date(2026, 1, 15), date(2026, 7, 14)), 5 / 12)
		self.assertIsNone(leave_entitlement.rule_for(rules, "Sick", leave_entitlement.service_years(date(2026, 1, 15), date(2026, 7, 14))))
		self.assertEqual(leave_entitlement.rule_for(rules, "Sick", leave_entitlement.service_years(date(2026, 1, 15), date(2026, 7, 15))).annual_entitlement, 5)


class TestPunchCleanup(unittest.TestCase):
	def test_same_second_in_out_is_one_punch_in_alternating_mode(self):
		logs = [log("2026-10-14 08:03", "IN"), log("2026-10-14 08:03", "OUT"), log("2026-10-14 17:59", "IN"), log("2026-10-14 17:59", "OUT")]
		intervals, miss_in, miss_out = build_intervals(logs, None)
		self.assertEqual(intervals, [(dt("2026-10-14 08:03"), dt("2026-10-14 17:59"))])
		self.assertFalse(miss_in or miss_out)


class TestShiftCalculationMode(unittest.TestCase):
	def test_first_last_mode_spans_all_punches(self):
		logs = [log("2026-11-05 08:47"), log("2026-11-05 15:42"), log("2026-11-05 17:30")]
		intervals, miss_in, miss_out = build_intervals(logs, None, "First Check-in and Last Check-out")
		self.assertEqual(intervals, [(dt("2026-11-05 08:47"), dt("2026-11-05 17:30"))])
		self.assertFalse(miss_in or miss_out)

	def test_first_last_single_punch_is_missing(self):
		intervals, _mi, miss_out = build_intervals([log("2026-11-05 08:47")], None, "First Check-in and Last Check-out")
		self.assertEqual(intervals, [])
		self.assertTrue(miss_out)

	def test_every_valid_mode_flags_unpaired(self):
		logs = [log("2026-11-05 08:47"), log("2026-11-05 15:42"), log("2026-11-05 17:30")]
		intervals, _mi, miss_out = build_intervals(logs, None, "Every Valid Check-in and Check-out")
		self.assertEqual(len(intervals), 1)
		self.assertTrue(miss_out)
