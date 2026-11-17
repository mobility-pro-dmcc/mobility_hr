"""Single source of truth for attendance periods and weeks.

Every module that needs "the month" or "the week" for a date goes through here, so a
custom period (e.g. 26th → 25th) is applied consistently.

Convention: a custom period is *named after the month in which it ends*.
With start day 26, period (2026, 11) = 2026-10-26 … 2026-11-25.
With start day 1 (calendar month), period (2026, 11) = 2026-11-01 … 2026-11-30.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from frappe.utils import getdate

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class Period:
	start: date
	end: date
	year: int
	month: int

	@property
	def label(self) -> str:
		return f"{self.year}-{self.month:02d}"

	def contains(self, d) -> bool:
		return self.start <= getdate(d) <= self.end

	def as_dict(self) -> dict:
		return {"start": str(self.start), "end": str(self.end), "year": self.year, "month": self.month, "label": self.label}


def normalize_start_day(start_day) -> int:
	start_day = int(start_day or 1)
	if not 1 <= start_day <= 28:
		raise ValueError("Month start day must be between 1 and 28")
	return start_day


def get_period(year: int, month: int, start_day: int = 1) -> Period:
	"""Period named (year, month)."""
	start_day = normalize_start_day(start_day)
	if start_day == 1:
		start = date(year, month, 1)
		end = start + relativedelta(months=1) - timedelta(days=1)
	else:
		end = date(year, month, start_day - 1)
		start = end - relativedelta(months=1) + timedelta(days=1)
	return Period(start, end, year, month)


def get_period_for_date(d, start_day: int = 1) -> Period:
	d = getdate(d)
	start_day = normalize_start_day(start_day)
	if start_day == 1 or d.day < start_day:
		return get_period(d.year, d.month, start_day)
	nxt = date(d.year, d.month, 1) + relativedelta(months=1)
	return get_period(nxt.year, nxt.month, start_day)


def get_week(d, week_start_day: str = "Saturday") -> tuple[date, date]:
	d = getdate(d)
	offset = (d.weekday() - WEEKDAYS.index(week_start_day or "Saturday")) % 7
	start = d - timedelta(days=offset)
	return start, start + timedelta(days=6)


def iter_dates(start, end):
	d, end = getdate(start), getdate(end)
	while d <= end:
		yield d
		d += timedelta(days=1)


def weekday_name(d) -> str:
	return WEEKDAYS[getdate(d).weekday()]


def handbook_start_day(handbook) -> int:
	if handbook and handbook.get("attendance_period_type") == "Custom Period":
		return normalize_start_day(handbook.get("period_start_day"))
	return 1
