"""Overtime and holiday-work calculation from actual check-in intervals.

Working day
- "Work After Shift End": only time worked after the shift end (optionally before the
  shift start) is overtime, so normal hours can never be counted.
- "Worked Hours Exceeding Shift Hours": worked − shift hours; the excess is taken from the
  end of the day's last intervals (used for the day/night split).
- The first `min_overtime_minutes` (buffer of the matching rule) are not overtime.
Weekly Off / Public Holiday
- Every worked minute is holiday work.
Then, per rule (day type × Day/Night, falling back to "Any"):
rounding → minimum eligible duration → max hours per day → payable hours / amount.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from frappe.utils import cint, flt, get_time

from mobility_hr.hr_policy.day_facts import DayFacts, clip_intervals, minutes

ROUNDING = {
	"Down to 15 Minutes": ("down", 15),
	"Down to 30 Minutes": ("down", 30),
	"Down to 60 Minutes": ("down", 60),
	"Nearest 15 Minutes": ("nearest", 15),
	"Nearest 30 Minutes": ("nearest", 30),
}


@dataclass
class OvertimeLine:
	rule: object
	time_of_day: str
	intervals: list
	raw_minutes: int
	counted_minutes: int = 0
	payable_hours: float = 0
	amount: float = 0


@dataclass
class OvertimeSpec:
	employee: str
	handbook: str
	date: object
	day_type: str
	facts: DayFacts
	lines: list = field(default_factory=list)
	comp_leave_days: float = 0
	compensation: str = "Monetary"
	notes: list = field(default_factory=list)

	@property
	def key(self) -> str:
		return f"{self.employee}|{self.date}"

	@property
	def counted_minutes(self) -> int:
		return sum(line.counted_minutes for line in self.lines)

	@property
	def payable_hours(self) -> float:
		return round(sum(line.payable_hours for line in self.lines), 4)

	@property
	def amount(self) -> float:
		return round(sum(line.amount for line in self.lines), 2)


def apply_rounding(mins: int, method: str | None) -> int:
	if method not in ROUNDING:
		return mins
	how, step = ROUNDING[method]
	if how == "down":
		return (mins // step) * step
	return int(round(mins / step)) * step


def take_from_end(intervals, mins: int) -> list:
	"""Last `mins` minutes of the intervals."""
	result, remaining = [], timedelta(minutes=mins)
	for a, b in reversed(intervals):
		if remaining <= timedelta(0):
			break
		length = b - a
		if length <= remaining:
			result.insert(0, (a, b))
			remaining -= length
		else:
			result.insert(0, (b - remaining, b))
			remaining = timedelta(0)
	return result


def drop_from_start(intervals, mins: int) -> list:
	"""Intervals without their first `mins` minutes (buffer)."""
	result, remaining = [], timedelta(minutes=mins)
	for a, b in intervals:
		length = b - a
		if remaining >= length:
			remaining -= length
			continue
		result.append((a + remaining, b))
		remaining = timedelta(0)
	return result


def night_windows(handbook, day) -> list:
	if not (handbook.night_start and handbook.night_end):
		return []
	ns, ne = get_time(handbook.night_start), get_time(handbook.night_end)
	windows = []
	for offset in (-1, 0, 1):
		d = day + timedelta(days=offset)
		start = datetime.combine(d, ns)
		end = datetime.combine(d, ne)
		if end <= start:
			end += timedelta(days=1)
		windows.append((start, end))
	return windows


def split_day_night(intervals, windows) -> dict:
	if not windows:
		return {"Day": intervals, "Night": []}
	night = []
	for w in windows:
		night.extend(clip_intervals(intervals, w[0], w[1]))
	day = []
	for a, b in intervals:
		cuts = sorted([(max(a, n0), min(b, n1)) for n0, n1 in night if min(b, n1) > max(a, n0)])
		cursor = a
		for c0, c1 in cuts:
			if c0 > cursor:
				day.append((cursor, c0))
			cursor = max(cursor, c1)
		if cursor < b:
			day.append((cursor, b))
	return {"Day": day, "Night": night}


def find_rule(rules, day_type: str, time_of_day: str):
	enabled = [r for r in rules or [] if r.enabled and r.day_type == day_type]
	for r in enabled:
		if r.time_of_day == time_of_day:
			return r
	for r in enabled:
		if (r.time_of_day or "Any") == "Any":
			return r
	return None


def overtime_intervals(facts: DayFacts, handbook, rules) -> list:
	if facts.holiday_type:
		return list(facts.intervals)
	if not (facts.shift_start and facts.shift_end):
		return []
	if handbook.overtime_basis == "Worked Hours Exceeding Shift Hours":
		excess = facts.worked_minutes - facts.standard_minutes
		ot = take_from_end(facts.intervals, excess) if excess > 0 else []
	else:
		ot = clip_intervals(facts.intervals, lo=facts.shift_end)
		if handbook.count_early_arrival:
			ot = clip_intervals(facts.intervals, hi=facts.shift_start) + ot
	buffer = max([cint(r.min_overtime_minutes) for r in rules or [] if r.enabled and r.day_type == "Working Day"] or [0])
	return drop_from_start(ot, buffer) if buffer else ot


def calculate(facts: DayFacts, handbook) -> OvertimeSpec | None:
	day_type = facts.holiday_type or "Working Day"
	holiday_comp = handbook.holiday_work_compensation or "None"
	monetary = bool(handbook.enable_overtime) and (day_type == "Working Day" or "Monetary" in holiday_comp)
	comp_leave = day_type != "Working Day" and "Compensatory" in holiday_comp

	if not (monetary or comp_leave) or not facts.intervals:
		return None

	spec = OvertimeSpec(employee=facts.employee, handbook=handbook.name, date=facts.date, day_type=day_type, facts=facts)
	rules = handbook.overtime_rules

	if monetary:
		ot = overtime_intervals(facts, handbook, rules)
		for tod, ivals in split_day_night(ot, night_windows(handbook, facts.date)).items():
			raw = minutes(ivals)
			if raw <= 0:
				continue
			rule = find_rule(rules, day_type, tod)
			if not rule:
				spec.notes.append(f"{raw} {tod.lower()} minutes on a {day_type} have no matching overtime rule and were not counted.")
				continue
			spec.lines.append(OvertimeLine(rule=rule, time_of_day=tod, intervals=ivals, raw_minutes=raw))

		# merge lines that resolved to the same rule (e.g. "Any") before rounding/thresholds
		merged = {}
		for line in spec.lines:
			key = line.rule.name or id(line.rule)
			if key in merged:
				merged[key].intervals += line.intervals
				merged[key].raw_minutes += line.raw_minutes
				if merged[key].time_of_day != line.time_of_day:
					merged[key].time_of_day = "Any"
			else:
				merged[key] = line
		spec.lines = list(merged.values())

		for line in spec.lines:
			r = line.rule
			counted = apply_rounding(line.raw_minutes, r.rounding_method)
			if cint(r.min_eligible_minutes) and counted < cint(r.min_eligible_minutes):
				spec.notes.append(f"{r.overtime_category}: {counted} min is below the minimum of {cint(r.min_eligible_minutes)} min.")
				counted = 0
			if flt(r.max_hours_per_day) and counted > flt(r.max_hours_per_day) * 60:
				spec.notes.append(f"{r.overtime_category}: capped at {flt(r.max_hours_per_day):g} h/day.")
				counted = int(flt(r.max_hours_per_day) * 60)
			line.counted_minutes = counted
			hours = counted / 60
			if r.calculation_method == "Fixed Hourly Rate":
				line.amount = round(hours * flt(r.fixed_rate), 2)
				line.payable_hours = round(hours, 4)
			else:
				line.payable_hours = round(hours * flt(r.multiplier or 1), 4)
		spec.lines = [line for line in spec.lines if line.counted_minutes > 0]

	if comp_leave:
		days = flt(handbook.comp_days_public_holiday if day_type == "Public Holiday" else handbook.comp_days_weekly_off)
		min_full = flt(handbook.min_hours_for_full_comp_day)
		if min_full and facts.worked_minutes < min_full * 60:
			days = days / 2
			spec.notes.append(f"Worked {facts.worked_minutes / 60:.2f} h (< {min_full:g} h): half compensation.")
		spec.comp_leave_days = days

	if spec.lines and spec.comp_leave_days:
		spec.compensation = "Monetary and Compensatory Leave"
	elif spec.comp_leave_days:
		spec.compensation = "Compensatory Leave"
	elif spec.lines:
		spec.compensation = "Monetary"
	else:
		return None
	return spec
