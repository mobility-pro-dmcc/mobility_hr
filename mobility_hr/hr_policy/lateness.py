"""Late attendance: counted late minutes per day and penalty rule matching.

Accumulation rules
- A day counts only if attendance was processed as Present / Work From Home / Half Day and
  it is not a holiday.
- Raw late = first check-in − shift start. Approved permission time inside that window is
  removed (permission time is never late time).
- Grace: "Ignore … within grace" → 0 if net late ≤ grace, else the full net minutes;
  "Subtract grace" → max(net − grace, 0).
- Daily rules evaluate each day; Weekly / Monthly rules evaluate the sum of counted
  minutes in a *completed* week (Handbook "Week Starts On") / attendance period.
- When several rules of one frequency match, the one with the highest minimum wins
  (tiers), so a period is penalised at most once per frequency.
"""

from dataclasses import dataclass, field
from datetime import date

from frappe.utils import cint, flt

from mobility_hr.hr_policy.day_facts import DayFacts

SUBTRACT_GRACE = "Subtract grace from late minutes"

UNITS = {
	"Fixed Amount": "Currency",
	"Working Hours": "Hours",
	"Leave Days": "Days",
	"Day's Salary Fraction": "Fraction of Daily Salary",
	"Warning Only": "None",
}


@dataclass
class LateDay:
	date: date
	attendance: str | None
	shift: str | None
	shift_start: object
	first_in: object
	raw: int
	permission: int
	grace: int
	counted: int


@dataclass
class PenaltySpec:
	employee: str
	handbook: str
	frequency: str
	period_start: date
	period_end: date
	total_minutes: int
	rule: object
	days: list = field(default_factory=list)

	@property
	def key(self) -> str:
		return f"{self.employee}|{self.frequency}|{self.period_start}"


def grace_for(facts: DayFacts, handbook) -> int:
	source = handbook.late_grace_source or "Shift Type Grace Period"
	if source == "Handbook Grace Period":
		return cint(handbook.late_grace_minutes)
	if source == "No Grace Period":
		return 0
	return cint(facts.late_grace_period)


def counted_late(raw: int, permission: int, grace: int, grace_handling: str | None) -> int:
	net = max(cint(raw) - cint(permission), 0)
	if grace_handling == SUBTRACT_GRACE:
		return max(net - grace, 0)
	return 0 if net <= grace else net


def late_day(facts: DayFacts, handbook) -> LateDay | None:
	"""Late info for a processed working day, or None when the day doesn't count."""
	if not facts.worked or facts.holiday_type or not facts.shift_start:
		return None
	grace = grace_for(facts, handbook)
	counted = counted_late(facts.raw_late_minutes, facts.late_permission_minutes, grace, handbook.grace_handling)
	return LateDay(
		date=facts.date,
		attendance=facts.attendance.name if facts.attendance else None,
		shift=facts.shift,
		shift_start=facts.shift_start,
		first_in=facts.first_in,
		raw=facts.raw_late_minutes,
		permission=facts.late_permission_minutes,
		grace=grace,
		counted=counted,
	)


def forgiven_minutes(day: LateDay) -> int:
	"""Late minutes that grace forgave on this day (net late − counted)."""
	return max(max(day.raw - day.permission, 0) - day.counted, 0)


def period_total(days: list[LateDay], grace_cap: int = 0) -> tuple[int, int]:
	"""(counted total for the period, forgiven minutes over the cap added back).

	With a cap (e.g. "grace of 10 min a day, not exceeding 40 accumulated minutes a month"),
	forgiven minutes beyond the cap are counted as late. Days are taken in date order, so the
	first forgiven minutes of the period use up the allowance.
	"""
	total = sum(d.counted for d in days)
	if not grace_cap or grace_cap <= 0:
		return total, 0
	forgiven = sum(forgiven_minutes(d) for d in sorted(days, key=lambda d: d.date))
	over = max(forgiven - grace_cap, 0)
	return total + over, over


def rule_matches(rule, minutes: int) -> bool:
	if not rule.enabled:
		return False
	lo = cint(rule.min_minutes)
	if rule.condition == "Greater Than":
		return minutes > lo
	hi = cint(rule.max_minutes)
	return lo <= minutes <= hi


def match_rule(rules, frequency: str, minutes: int):
	"""Most severe matching rule (highest minimum, then highest value) or None."""
	if minutes <= 0:
		return None
	candidates = [r for r in rules or [] if r.frequency == frequency and rule_matches(r, minutes)]
	if not candidates:
		return None
	return max(candidates, key=lambda r: (cint(r.min_minutes), flt(r.penalty_value)))


def describe_rule(rule) -> str:
	if rule.condition == "Greater Than":
		cond = f"more than {cint(rule.min_minutes)} min"
	else:
		cond = f"{cint(rule.min_minutes)}–{cint(rule.max_minutes)} min"
	unit = UNITS.get(rule.penalty_type, "")
	value = "" if rule.penalty_type == "Warning Only" else f" {flt(rule.penalty_value):g} {unit}"
	return f"{rule.rule_name} ({rule.frequency}, {cond}) → {rule.penalty_type}{value}"
