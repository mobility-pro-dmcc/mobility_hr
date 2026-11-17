"""Shared logic for biometric devices: store raw punches, convert them to Employee Checkins."""

from datetime import datetime, timedelta

import frappe
from frappe.utils import add_days, cint, get_datetime, now_datetime

PUSH = "Push (ADMS / Cloud Server)"
PULL = "Pull (TCP/IP)"

LOG_DOCTYPE = "Biometric Attendance Log"
DEVICE_DOCTYPE = "Biometric Device"

# ZKTeco punch states → Frappe HR log type
IN_STATES = {0, 3, 4}  # Check-In, Break-In, OT-In
OUT_STATES = {1, 2, 5}  # Check-Out, Break-Out, OT-Out


def make_log_key(device: str, user_id: str, timestamp: datetime) -> str:
	return f"{device}|{user_id}|{timestamp:%Y-%m-%d %H:%M:%S}"


def ingest_records(device_name: str, records: list[dict]) -> dict:
	"""Save raw punches from a device and queue them for processing.

	Each record: {"user_id": str, "timestamp": datetime, "punch_status": int, "verify_type": int}
	Returns counts: {"received", "new"}.
	"""
	device = frappe.get_cached_doc(DEVICE_DOCTYPE, device_name)
	offset = timedelta(minutes=cint(device.time_offset_minutes))

	prepared = {}
	for r in records:
		user_id = str(r.get("user_id") or "").strip()
		ts = r.get("timestamp")
		if not user_id or not ts:
			continue
		ts = get_datetime(ts).replace(microsecond=0) + offset
		key = make_log_key(device.name, user_id, ts)
		prepared[key] = (user_id, ts, r)

	if not prepared:
		return {"received": len(records), "new": 0}

	existing = set()
	keys = list(prepared)
	for i in range(0, len(keys), 500):
		existing.update(
			frappe.get_all(LOG_DOCTYPE, filters={"log_key": ["in", keys[i : i + 500]]}, pluck="log_key")
		)

	new_count = 0
	latest = None
	for key, (user_id, ts, r) in prepared.items():
		latest = ts if latest is None or ts > latest else latest
		if key in existing:
			continue
		log = frappe.new_doc(LOG_DOCTYPE)
		log.update(
			{
				"device": device.name,
				"user_id": user_id,
				"timestamp": ts,
				"punch_status": cint(r.get("punch_status")),
				"verify_type": cint(r.get("verify_type")),
				"log_key": key,
				"status": "Pending",
			}
		)
		try:
			log.insert(ignore_permissions=True)
			new_count += 1
		except frappe.DuplicateEntryError:
			# Same punch arriving in parallel requests
			pass

	updates = {}
	if latest and (not device.last_log_time or latest > get_datetime(device.last_log_time)):
		updates["last_log_time"] = latest
	if updates:
		frappe.db.set_value(DEVICE_DOCTYPE, device.name, updates, update_modified=False)

	if new_count:
		enqueue_processing(device.name)

	return {"received": len(records), "new": new_count}


def enqueue_processing(device_name: str | None = None):
	frappe.enqueue(
		"mobility_hr.biometric.processing.process_pending_logs",
		queue="short",
		device=device_name,
		job_id=f"biometric_process::{device_name or 'all'}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def skip_before_import_date(device: str | None = None, statuses=("Pending",)) -> int:
	"""Mark punches older than the device's 'Import Logs From' as Skipped in one update.

	Avoids processing thousands of old punches one by one (which kept recent punches waiting).
	"""
	devices = frappe.get_all(
		DEVICE_DOCTYPE,
		filters={"import_logs_from": ["is", "set"], **({"name": device} if device else {})},
		fields=["name", "import_logs_from"],
	)
	total = 0
	for d in devices:
		filters = {"device": d.name, "status": ["in", list(statuses)], "timestamp": ["<", d.import_logs_from]}
		count = frappe.db.count(LOG_DOCTYPE, filters)
		if count:
			frappe.db.set_value(
				LOG_DOCTYPE,
				filters,
				{"status": "Skipped", "error": "Punch is older than 'Import Logs From' on the device."},
				update_modified=True,
			)
			total += count
	return total


def process_pending_logs(device: str | None = None, statuses: tuple | list = ("Pending",), limit: int = 5000):
	"""Convert stored punches to Employee Checkins. Safe to run repeatedly."""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")

	result = {}
	skipped = skip_before_import_date(device, statuses)
	if skipped:
		result["Skipped"] = skipped
		frappe.db.commit()

	filters = {"status": ["in", list(statuses)]}
	if device:
		filters["device"] = device

	names = frappe.get_all(LOG_DOCTYPE, filters=filters, order_by="timestamp asc", limit=limit, pluck="name")
	for name in names:
		status = process_log(name)
		result[status] = result.get(status, 0) + 1
		frappe.db.commit()
	return result


def process_log(log_name: str) -> str:
	# Lock the row so two processors (background job + "Retry" button / scheduler) can't
	# both create a checkin for the same punch. The second waits, then sees it is done.
	current = frappe.db.get_value(LOG_DOCTYPE, log_name, "status", for_update=True)
	if current in ("Processed", "Duplicate"):
		return current

	log = frappe.get_doc(LOG_DOCTYPE, log_name)
	device = frappe.get_cached_doc(DEVICE_DOCTYPE, log.device)

	def finish(status, error=None, **values):
		values.update({"status": status, "error": (error or "")[:1000] or None})
		log.db_set(values, update_modified=True)
		return status

	if device.import_logs_from and get_datetime(log.timestamp) < get_datetime(device.import_logs_from):
		return finish("Skipped", "Punch is older than 'Import Logs From' on the device.")

	employee = frappe.db.get_value(
		"Employee", {"attendance_device_id": log.user_id}, ["name", "employee_name", "status"], as_dict=True
	)
	if not employee:
		return finish(
			"Employee Not Found",
			f"No Employee has Attendance Device ID = {log.user_id}. Set it on the Employee record; "
			"the punch will be retried automatically.",
		)

	log_type = get_log_type(device.log_type_mode, log.punch_status)

	existing = frappe.db.get_value("Employee Checkin", {"employee": employee.name, "time": log.timestamp}, "name")
	if existing:
		return finish("Duplicate", employee=employee.name, employee_checkin=existing, log_type=log_type)

	savepoint = "biometric_checkin"
	frappe.db.savepoint(savepoint)
	try:
		checkin = frappe.new_doc("Employee Checkin")
		checkin.update(
			{
				"employee": employee.name,
				"employee_name": employee.employee_name,
				"time": log.timestamp,
				"device_id": device.device_location or device.name,
				"log_type": log_type,
				"latitude": device.latitude or None,
				"longitude": device.longitude or None,
			}
		)
		checkin.insert(ignore_permissions=True)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.clear_last_message()
		return finish("Failed", _plain_error(e), employee=employee.name, log_type=log_type)

	return finish("Processed", employee=employee.name, employee_checkin=checkin.name, log_type=log_type)


def get_log_type(mode: str | None, punch_status) -> str | None:
	if mode == "All Punches are IN":
		return "IN"
	if mode == "All Punches are OUT":
		return "OUT"
	if mode == "From Device Punch State":
		state = cint(punch_status)
		if state in IN_STATES:
			return "IN"
		if state in OUT_STATES:
			return "OUT"
	return None


def _plain_error(e: Exception) -> str:
	from frappe.utils import strip_html

	return strip_html(str(e)) or e.__class__.__name__


def touch_device(device_name: str, ip: str | None = None, **extra):
	"""Record that the device contacted us. Throttled to one DB write per minute unless extra values are given."""
	cache_key = f"biometric_device_seen::{device_name}"
	if not extra and frappe.cache.get_value(cache_key, expires=True):
		return
	values = {"last_activity": now_datetime(), **extra}
	if ip:
		values["last_ip"] = ip
	frappe.db.set_value(DEVICE_DOCTYPE, device_name, values, update_modified=False)
	frappe.cache.set_value(cache_key, 1, expires_in_sec=60)


# ---------------------------------------------------------------- scheduler


def scheduled_processing():
	"""Every 10 minutes: process anything left pending and retry punches whose employee was missing."""
	process_pending_logs(statuses=("Pending",))
	# Attendance Device IDs are often set after the employee starts punching; retry the last 31 days.
	names = frappe.get_all(
		LOG_DOCTYPE,
		filters={"status": "Employee Not Found", "timestamp": [">=", add_days(now_datetime(), -31)]},
		pluck="name",
		limit=5000,
	)
	if not names:
		return
	device_ids = set(
		frappe.get_all("Employee", filters={"attendance_device_id": ["is", "set"]}, pluck="attendance_device_id")
	)
	for name in names:
		if frappe.db.get_value(LOG_DOCTYPE, name, "user_id") in device_ids:
			process_log(name)
			frappe.db.commit()


def reprocess_device_job(device: str, statuses: list, user: str | None = None):
	"""Background job for the 'Retry Unprocessed Punches' button: runs until nothing is left."""
	totals: dict[str, int] = {}
	while True:
		before = sum(totals.values())
		for status, n in process_pending_logs(device=device, statuses=statuses).items():
			totals[status] = totals.get(status, 0) + n
		# stop when a pass changed nothing (remaining punches are e.g. still without an employee)
		remaining = frappe.db.count(LOG_DOCTYPE, {"device": device, "status": "Pending"})
		if not remaining or sum(totals.values()) == before or sum(totals.values()) - before < 5000:
			break

	missing = frappe.get_all(
		LOG_DOCTYPE,
		filters={"device": device, "status": "Employee Not Found"},
		fields=["user_id", "count(name) as n"],
		group_by="user_id",
		order_by="n desc",
	)
	parts = [f"{k}: {v}" for k, v in totals.items()] or ["Nothing to process."]
	message = f"{device}: " + ", ".join(parts)
	if missing:
		message += ". No employee has Attendance Device ID " + ", ".join(m.user_id for m in missing) + "."
	if user:
		frappe.publish_realtime(
			"biometric_sync_done", {"device": device, "success": True, "message": message}, user=user, after_commit=True
		)
	return totals
