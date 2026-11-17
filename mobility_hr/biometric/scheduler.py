"""Runs every minute (see hooks.py) and triggers device syncs per Biometric Settings."""

from datetime import datetime, timedelta

import frappe
from frappe.utils import cint, get_datetime, get_time, now_datetime

from mobility_hr.biometric.processing import DEVICE_DOCTYPE, PULL, PUSH

SETTINGS = "Biometric Settings"
# A schedule that was missed (server down, scheduler paused) still runs if we are within this window.
CATCH_UP_WINDOW = timedelta(hours=2)


def run():
	settings = frappe.get_single(SETTINGS)
	now = now_datetime()
	run_interval_sync(settings, now)
	run_time_schedules(settings, now)


def run_interval_sync(settings, now: datetime):
	if not settings.enable_interval_sync:
		return
	interval = max(cint(settings.sync_interval_minutes), 1)
	for name in frappe.get_all(DEVICE_DOCTYPE, filters={"enabled": 1, "connection_type": PULL}, pluck="name"):
		key = f"biometric_interval_sync::{name}"
		if frappe.cache.get_value(key, expires=True):
			continue
		frappe.cache.set_value(key, 1, expires_in_sec=interval * 60 - 5)
		enqueue_pull(name, full=False)


def run_time_schedules(settings, now: datetime):
	for row in settings.schedules:
		if not row.enabled or not row.sync_time:
			continue
		due = datetime.combine(now.date(), get_time(row.sync_time))
		if due > now:
			due -= timedelta(days=1)  # most recent occurrence
		if now - due > CATCH_UP_WINDOW:
			continue
		if row.last_run and get_datetime(row.last_run) >= due:
			continue

		run_schedule(row)
		frappe.db.set_value(row.doctype, row.name, "last_run", now, update_modified=False)


def run_schedule(row):
	filters = {"enabled": 1}
	if row.device:
		filters["name"] = row.device
	for device in frappe.get_all(DEVICE_DOCTYPE, filters=filters, fields=["name", "connection_type"]):
		if device.connection_type == PULL:
			enqueue_pull(device.name, full=cint(row.full_sync))
		elif device.connection_type == PUSH:
			request_push_upload(device.name, full=cint(row.full_sync))


def enqueue_pull(device_name: str, full: bool):
	frappe.enqueue(
		"mobility_hr.biometric.pull.sync_device",
		queue="long",
		timeout=1800,
		device_name=device_name,
		full=full,
		job_id=f"biometric_pull::{device_name}",
		deduplicate=True,
	)


def request_push_upload(device_name: str, full: bool):
	"""Ask a push device to (re)send punches on its next poll."""
	values = {"pending_command": "CHECK"}
	if full:
		values["attlog_stamp"] = None
	frappe.db.set_value(DEVICE_DOCTYPE, device_name, values, update_modified=False)
