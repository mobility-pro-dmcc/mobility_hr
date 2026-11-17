"""Pull mode: connect to the device over TCP/IP (port 4370) with pyzk and read attendance logs.

Requires the ERP server to reach the device's IP (same LAN, VPN or port forward).
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from mobility_hr.biometric.processing import DEVICE_DOCTYPE, PULL, ingest_records, touch_device


def _connect(device):
	try:
		from zk import ZK
	except ImportError:
		frappe.throw(_("The 'pyzk' Python package is not installed. Run: bench pip install pyzk"))

	zk = ZK(
		device.ip_address,
		port=cint(device.port) or 4370,
		timeout=cint(device.timeout) or 15,
		password=cint(device.comm_password),
		force_udp=bool(device.force_udp),
		ommit_ping=True,
	)
	return zk.connect()


def test_connection(device_name: str) -> dict:
	device = frappe.get_doc(DEVICE_DOCTYPE, device_name)
	conn = _connect(device)
	try:
		info = {
			"Firmware": conn.get_firmware_version(),
			"Serial Number": conn.get_serialnumber(),
			"Platform": conn.get_platform(),
			"Device Name": conn.get_device_name(),
			"Device Time": str(conn.get_time()),
		}
	finally:
		conn.disconnect()
	text = "\n".join(f"{k}: {v}" for k, v in info.items())
	values = {"device_info": text}
	if not device.serial_number and info.get("Serial Number"):
		values["serial_number"] = info["Serial Number"]
	touch_device(device.name, device.ip_address, **values)
	return info


def sync_device(device_name: str, full: bool = False) -> dict:
	device = frappe.get_doc(DEVICE_DOCTYPE, device_name)
	if device.connection_type != PULL:
		frappe.throw(_("{0} is not a Pull (TCP/IP) device.").format(device_name))

	try:
		conn = _connect(device)
		try:
			attendances = conn.get_attendance() or []
		finally:
			conn.disconnect()
	except Exception as e:
		frappe.db.set_value(
			DEVICE_DOCTYPE,
			device.name,
			"last_sync_status",
			f"{now_datetime():%Y-%m-%d %H:%M:%S} pull failed: {e}",
			update_modified=False,
		)
		frappe.db.commit()
		raise

	# Only look at punches around/after the newest one we already have; duplicates are skipped anyway.
	since = None
	if device.last_log_time and not cint(full):
		since = get_datetime(device.last_log_time) - timedelta(minutes=cint(device.time_offset_minutes)) - timedelta(days=1)

	records = [
		{
			"user_id": a.user_id,
			"timestamp": a.timestamp,
			"punch_status": a.punch,
			"verify_type": a.status,
		}
		for a in attendances
		if not since or a.timestamp >= since
	]
	result = ingest_records(device.name, records)
	touch_device(
		device.name,
		device.ip_address,
		last_sync_status=f"{now_datetime():%Y-%m-%d %H:%M:%S} {'full ' if cint(full) else ''}pull: {len(attendances)} on device, "
		f"{result['new']} new",
	)
	return result


def sync_device_job(device_name: str, full: bool = False, user: str | None = None):
	"""Background wrapper for the "Sync Now" button: runs the sync and tells the user how it went."""
	try:
		result = sync_device(device_name, full=full)
		payload = {
			"device": device_name,
			"success": True,
			"message": _("{0}: received {1} punches, {2} new. Converting to Employee Checkins.").format(
				device_name, result["received"], result["new"]
			),
		}
	except Exception as e:
		frappe.log_error(title=f"Biometric sync failed: {device_name}")
		payload = {"device": device_name, "success": False, "message": _("{0}: sync failed – {1}").format(device_name, e)}

	if user:
		frappe.publish_realtime("biometric_sync_done", payload, user=user, after_commit=True)


def sync_all_pull_devices():
	"""Kept for compatibility; scheduling now lives in mobility_hr.biometric.scheduler (Biometric Settings)."""
	from mobility_hr.biometric.scheduler import run

	run()
