"""Per-employee, per-day attendance facts built from existing HRMS records.

Everything that needs "what happened on day X" (lateness, overtime, half-day rules, WFH,
dashboard) uses `get_day_facts`, so shifts, holidays, check-in pairing and permission
windows are interpreted in exactly one place. All data for a date range is loaded with a
handful of bulk queries.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import frappe

from mobility_hr.hr_policy.cache import clear, request_cache
from frappe.utils import get_time, getdate, now_datetime

from mobility_hr.hr_policy.handbook import get_employee
from mobility_hr.hr_policy.periods import iter_dates, weekday_name
from mobility_hr.hr_policy.permissions import get_approved_permissions, overlap_minutes, windows_for_date

# Punches closer than this are treated as one (double taps on the device).
DUPLICATE_PUNCH_SECONDS = 60
STRICT_LOG_TYPE = "Strictly based on Log Type in Employee Checkin"
WORKED_STATUSES = ("Present", "Work From Home", "Half Day")


@dataclass
class DayFacts:
	employee: str
	date: date
	weekday: str
	shift: str | None = None
	shift_start: datetime | None = None
	shift_end: datetime | None = None
	standard_minutes: int = 0
	late_grace_period: int = 0
	holiday_type: str | None = None  # None | "Weekly Off" | "Public Holiday"
	holiday_description: str | None = None
	is_wfh_day: bool = False
	wfh_rule: str | None = None
	attendance: frappe._dict | None = None
	leave: frappe._dict | None = None
	checkins: list = field(default_factory=list)
	intervals: list = field(default_factory=list)  # [(in, out)]
	first_in: datetime | None = None
	last_out: datetime | None = None
	worked_minutes: int = 0
	missing_check_in: bool = False
	missing_check_out: bool = False
	permissions: list = field(default_factory=list)  # [(start, end, permission)]
	raw_late_minutes: int = 0
	late_permission_minutes: int = 0
	early_exit_minutes: int = 0
	early_permission_minutes: int = 0

	@property
	def is_working_day(self) -> bool:
		return bool(self.shift) and not self.holiday_type

	@property
	def attendance_status(self) -> str | None:
		return self.attendance.status if self.attendance else None

	@property
	def worked(self) -> bool:
		return self.attendance_status in WORKED_STATUSES

	def permission_minutes_in_shift(self) -> int:
		"""Permission time inside the shift that was not worked (legitimate absence)."""
		if not (self.shift_start and self.shift_end and self.permissions):
			return 0
		shift = [(self.shift_start, self.shift_end)]
		in_shift = overlap_minutes(self.permissions, shift)
		worked_during_permission = overlap_minutes(
			[(max(a, self.shift_start), min(b, self.shift_end)) for a, b, _ in self.permissions if b > a],
			self.intervals,
		)
		return max(in_shift - worked_during_permission, 0)


# --------------------------------------------------------------------------- loaders


def get_shift_type(name: str) -> frappe._dict:
	cache = request_cache("hr_policy_shift_cache")
	if name not in cache:
		st = frappe.get_cached_doc("Shift Type", name)
		cache[name] = frappe._dict(
			name=st.name,
			start_time=st.start_time,
			end_time=st.end_time,
			holiday_list=st.holiday_list,
			late_entry_grace_period=st.late_entry_grace_period or 0,
			determine_check_in_and_check_out=st.determine_check_in_and_check_out,
			working_hours_calculation_based_on=st.working_hours_calculation_based_on,
			wfh_attendance_rule=st.get("wfh_attendance_rule") or "Require Check-in",
			wfh_days={row.weekday for row in (st.get("wfh_days") or [])},
		)
	return cache[name]


def shift_window(shift_type: frappe._dict, d: date) -> tuple[datetime, datetime]:
	start = datetime.combine(d, get_time(shift_type.start_time))
	end = datetime.combine(d, get_time(shift_type.end_time))
	if end <= start:  # overnight shift ends next day
		end += timedelta(days=1)
	return start, end


def get_shift_map(employee: str, start: date, end: date) -> dict[date, str]:
	"""Shift Type per date: active Shift Assignment first, else the employee's default shift."""
	emp = get_employee(employee)
	assignments = frappe.get_all(
		"Shift Assignment",
		filters={"employee": employee, "docstatus": 1, "status": "Active", "start_date": ["<=", end]},
		or_filters=[["end_date", "is", "not set"], ["end_date", ">=", start]],
		fields=["shift_type", "start_date", "end_date"],
		order_by="start_date asc",
	)
	result = {}
	for d in iter_dates(start, end):
		shift = None
		for a in assignments:
			if getdate(a.start_date) <= d and (not a.end_date or d <= getdate(a.end_date)):
				shift = a.shift_type
				break
		result[d] = shift or emp.default_shift
	return result


def get_holiday_list(employee: str, shift: str | None) -> str | None:
	if shift:
		hl = get_shift_type(shift).holiday_list
		if hl:
			return hl
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

	return get_holiday_list_for_employee(employee, raise_exception=False)


def get_holidays(holiday_list: str, start: date, end: date) -> dict[date, frappe._dict]:
	if not holiday_list:
		return {}
	cache = request_cache("hr_policy_holiday_cache")
	key = (holiday_list, start, end)
	if key not in cache:
		rows = frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "parenttype": "Holiday List", "holiday_date": ["between", [start, end]]},
			fields=["holiday_date", "weekly_off", "description"],
		)
		cache[key] = {getdate(r.holiday_date): r for r in rows}
	return cache[key]


def get_attendance_map(employee: str, start: date, end: date) -> dict[date, frappe._dict]:
	rows = frappe.get_all(
		"Attendance",
		filters={"employee": employee, "docstatus": 1, "attendance_date": ["between", [start, end]]},
		fields=[
			"name",
			"attendance_date",
			"status",
			"working_hours",
			"in_time",
			"out_time",
			"shift",
			"leave_type",
			"leave_application",
			"late_entry",
			"early_exit",
			"half_day_status",
		],
	)
	return {getdate(r.attendance_date): r for r in rows}


def get_leave_map(employee: str, start: date, end: date) -> dict[date, frappe._dict]:
	rows = frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee,
			"docstatus": 1,
			"status": "Approved",
			"from_date": ["<=", end],
			"to_date": [">=", start],
		},
		fields=["name", "leave_type", "from_date", "to_date", "half_day", "half_day_date"],
	)
	result = {}
	for r in rows:
		for d in iter_dates(max(getdate(r.from_date), start), min(getdate(r.to_date), end)):
			half = bool(r.half_day and r.half_day_date and getdate(r.half_day_date) == d)
			result[d] = frappe._dict(name=r.name, leave_type=r.leave_type, half_day=half)
	return result


def get_checkins(employee: str, start: date, end: date) -> list[frappe._dict]:
	return frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": employee,
			"time": ["between", [datetime.combine(start - timedelta(days=1), time.min), datetime.combine(end + timedelta(days=2), time.min)]],
		},
		fields=["name", "time", "log_type", "shift", "shift_start", "attendance", "skip_auto_attendance"],
		order_by="time asc",
	)


# --------------------------------------------------------------------------- interval logic


def dedupe_punches(logs: list, strict: bool = False) -> list:
	"""Merge punches less than DUPLICATE_PUNCH_SECONDS apart (double taps, duplicate uploads).

	In alternating mode the IN/OUT label carries no meaning, so a same-second IN+OUT pair is one
	punch. In strict mode only punches with the same label are merged.
	"""
	result = []
	for log in sorted(logs, key=lambda log: log.time):
		if result and (log.time - result[-1].time).total_seconds() < DUPLICATE_PUNCH_SECONDS:
			if not strict or log.log_type == result[-1].log_type:
				continue
		result.append(log)
	return result


FIRST_LAST = "First Check-in and Last Check-out"


def build_intervals(logs: list, mode: str | None, calc: str | None = None) -> tuple[list, bool, bool]:
	"""Pair punches into (in, out) intervals. Returns (intervals, missing_in, missing_out).

	Follows the Shift Type exactly like HRMS does:
	- calc "First Check-in and Last Check-out": one span from the first IN to the last OUT; a punch is
	  only missing when there is a single punch (alternating) or no IN / no OUT at all (strict).
	- calc "Every Valid Check-in and Check-out": consecutive IN/OUT pairs; an unpaired punch is missing.
	"""
	logs = dedupe_punches(logs, strict=mode == STRICT_LOG_TYPE)
	intervals, missing_in, missing_out = [], False, False
	if not logs:
		return intervals, missing_in, missing_out

	if calc == FIRST_LAST:
		if mode == STRICT_LOG_TYPE and all(log.log_type for log in logs):
			ins = [log.time for log in logs if log.log_type == "IN"]
			outs = [log.time for log in logs if log.log_type == "OUT"]
			missing_in, missing_out = not ins, not outs
			if ins and outs and max(outs) > min(ins):
				intervals.append((min(ins), max(outs)))
		elif len(logs) >= 2:
			intervals.append((logs[0].time, logs[-1].time))
		else:
			missing_out = True
		return intervals, missing_in, missing_out

	if mode == STRICT_LOG_TYPE and all(log.log_type for log in logs):
		current_in = None
		for log in logs:
			if log.log_type == "IN":
				if current_in is not None:
					missing_out = True  # IN, IN → the first had no OUT
				current_in = log.time
			else:
				if current_in is None:
					missing_in = True
				else:
					intervals.append((current_in, log.time))
					current_in = None
		if current_in is not None:
			missing_out = True
	else:
		for i in range(0, len(logs) - 1, 2):
			intervals.append((logs[i].time, logs[i + 1].time))
		if len(logs) % 2:
			missing_out = True
	return intervals, missing_in, missing_out


def clip_intervals(intervals, lo: datetime | None = None, hi: datetime | None = None) -> list:
	out = []
	for a, b in intervals:
		a2 = max(a, lo) if lo else a
		b2 = min(b, hi) if hi else b
		if b2 > a2:
			out.append((a2, b2))
	return out


def minutes(intervals) -> int:
	return int(round(sum((b - a).total_seconds() for a, b in intervals) / 60))


# --------------------------------------------------------------------------- main entry


def get_day_facts(employee: str, start, end) -> list[DayFacts]:
	start, end = getdate(start), getdate(end)
	emp = get_employee(employee)
	shift_map = get_shift_map(employee, start, end)
	attendance_map = get_attendance_map(employee, start, end)
	leave_map = get_leave_map(employee, start, end)
	permissions = get_approved_permissions(employee, start, end)
	checkins = get_checkins(employee, start, end)

	# Group check-ins to the day whose shift they belong to (overnight-safe):
	# HRMS stores the shift start on each check-in; otherwise use the calendar date.
	logs_by_day: dict[date, list] = {}
	for log in checkins:
		if log.skip_auto_attendance:
			continue
		day = getdate(log.shift_start) if log.shift_start else getdate(log.time)
		logs_by_day.setdefault(day, []).append(log)

	holiday_lists: dict[str | None, dict] = {}
	result = []
	now = now_datetime()
	for d in iter_dates(start, end):
		f = DayFacts(employee=employee, date=d, weekday=weekday_name(d))
		if emp.date_of_joining and d < getdate(emp.date_of_joining):
			result.append(f)
			continue

		f.shift = shift_map.get(d)
		st = get_shift_type(f.shift) if f.shift else None
		if st:
			f.shift_start, f.shift_end = shift_window(st, d)
			f.standard_minutes = int((f.shift_end - f.shift_start).total_seconds() // 60)
			f.late_grace_period = st.late_entry_grace_period
			f.wfh_rule = st.wfh_attendance_rule

		hl = get_holiday_list(employee, f.shift)
		if hl not in holiday_lists:
			holiday_lists[hl] = get_holidays(hl, start, end)
		hol = holiday_lists[hl].get(d)
		if hol:
			f.holiday_type = "Weekly Off" if hol.weekly_off else "Public Holiday"
			f.holiday_description = hol.description

		f.is_wfh_day = bool(st and f.weekday in st.wfh_days and not f.holiday_type)
		f.attendance = attendance_map.get(d)
		f.leave = leave_map.get(d)

		f.checkins = logs_by_day.get(d, [])
		mode = st.determine_check_in_and_check_out if st else None
		calc = st.working_hours_calculation_based_on if st else None
		f.intervals, f.missing_check_in, f.missing_check_out = build_intervals(f.checkins, mode, calc)
		if f.missing_check_out and d == now.date():
			f.missing_check_out = False  # still at work
		f.worked_minutes = minutes(f.intervals)
		if f.checkins:
			punches = dedupe_punches(f.checkins, strict=mode == STRICT_LOG_TYPE)
			ins = [p.time for p in punches if p.log_type != "OUT"] if mode == STRICT_LOG_TYPE else [punches[0].time]
			outs = [p.time for p in punches if p.log_type == "OUT"] if mode == STRICT_LOG_TYPE else ([punches[-1].time] if len(punches) > 1 else [])
			f.first_in = min(ins) if ins else None
			f.last_out = max(outs) if outs else None

		f.permissions = windows_for_date(permissions, d)

		if f.shift_end and f.first_in and f.first_in >= f.shift_end and not f.holiday_type:
			# first punch after the shift ended: the morning check-in is missing, not 9 hours late
			f.missing_check_in = True
		elif f.shift_start and f.first_in and f.first_in > f.shift_start and not f.holiday_type:
			f.raw_late_minutes = int((f.first_in - f.shift_start).total_seconds() // 60)
			f.late_permission_minutes = min(overlap_minutes(f.permissions, [(f.shift_start, f.first_in)]), f.raw_late_minutes)
		if f.shift_end and f.last_out and f.last_out < f.shift_end and not f.holiday_type:
			f.early_exit_minutes = int((f.shift_end - f.last_out).total_seconds() // 60)
			f.early_permission_minutes = min(overlap_minutes(f.permissions, [(f.last_out, f.shift_end)]), f.early_exit_minutes)

		result.append(f)
	return result


def get_single_day(employee: str, d) -> DayFacts:
	return get_day_facts(employee, d, d)[0]


def clear_cache():
	clear("hr_policy_shift_cache", "hr_policy_holiday_cache")
