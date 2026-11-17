"""ZKTeco PUSH protocol (ADMS / "Cloud Server Setting") endpoint.

The device is configured with this server's address and talks plain HTTP to:

    GET  /iclock/cdata?SN=<sn>&options=all      handshake → server returns upload options
    POST /iclock/cdata?SN=<sn>&table=ATTLOG&Stamp=<n>
                                                 attendance lines: PIN\tYYYY-MM-DD HH:MM:SS\tstate\tverify\t...
                                                 → "OK: <count>"
    POST /iclock/cdata?SN=<sn>&table=OPERLOG     operation log → "OK"
    GET  /iclock/getrequest?SN=<sn>              command poll → "OK" or "C:<id>:<command>"
    POST /iclock/devicecmd?SN=<sn>               command result → "OK"
    GET/POST /iclock/registry, /iclock/push, /iclock/ping   (newer firmware)

Some firmware appends ".aspx" to these paths; that is handled too.
The device does not authenticate, so only serial numbers registered (and enabled) as a
Biometric Device are accepted. Unknown serial numbers are remembered so they can be
picked when creating the device in the desk.
"""

import re

import frappe
from frappe.utils import cint, now_datetime
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

from mobility_hr.biometric.processing import DEVICE_DOCTYPE, PUSH, ingest_records, touch_device

UNKNOWN_DEVICES_CACHE_KEY = "biometric_unregistered_devices"

ATTLOG_LINE = re.compile(
	r"^\s*(?P<pin>[^\t ]+)[\t ]+(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)"
	r"(?:[\t ]+(?P<state>-?\d+))?(?:[\t ]+(?P<verify>-?\d+))?"
)


class ZKTecoPushRenderer(BaseRenderer):
	"""Registered through the `page_renderer` hook so the device can use its fixed /iclock/* paths."""

	def can_render(self):
		return self.path.split("/", 1)[0].lower() == "iclock"

	def render(self):
		try:
			body, status = handle(self.path)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="ZKTeco push request failed")
			body, status = "ERROR", 500
		return Response(body, status=status, mimetype="text/plain")


def handle(path: str) -> tuple[str, int]:
	request = frappe.local.request
	endpoint = path.split("/", 1)[1].lower() if "/" in path else ""
	endpoint = endpoint.removesuffix(".aspx").strip("/")
	args = request.args
	sn = (args.get("SN") or args.get("sn") or "").strip()

	if not sn:
		return "ERROR: missing SN", 400

	device = frappe.db.get_value(
		DEVICE_DOCTYPE,
		{"serial_number": sn, "connection_type": PUSH},
		["name", "enabled", "attlog_stamp", "pending_command", "command_id"],
		as_dict=True,
	)
	ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()

	if not device or not device.enabled:
		if not device:
			remember_unknown_device(sn, ip, endpoint)
		# Reply "OK" so the device keeps polling quietly until it is registered/enabled.
		return "OK", 200

	# Writes made during GET requests must be committed explicitly in Frappe.
	frappe.local.flags.commit = True
	touch_device(device.name, ip)

	if endpoint == "cdata":
		if request.method == "GET":
			return handshake(sn, device), 200
		return receive_data(device, args, request.get_data(as_text=True)), 200

	if endpoint == "getrequest":
		if args.get("INFO"):
			touch_device(device.name, ip, device_info=format_info(args.get("INFO")))
		return pop_command(device), 200

	if endpoint == "devicecmd":
		touch_device(device.name, ip, last_sync_status=f"Command result: {request.get_data(as_text=True)[:500]}")
		return "OK", 200

	if endpoint == "registry":
		body = request.get_data(as_text=True)
		if body:
			touch_device(device.name, ip, device_info=body[:2000])
		return f"RegistryCode={frappe.generate_hash(sn, 10)}", 200

	if endpoint == "push":
		return handshake(sn, device), 200

	# ping, querydata, etc.
	return "OK", 200


def handshake(sn: str, device) -> str:
	"""Upload options sent to the device. ATTLOGStamp tells it which logs we already have."""
	stamp = device.attlog_stamp or "None"  # None → device uploads every log it holds
	return "\n".join(
		[
			f"GET OPTION FROM: {sn}",
			f"ATTLOGStamp={stamp}",
			"OPERLOGStamp=9999",
			"ATTPHOTOStamp=None",
			"ErrorDelay=30",
			"Delay=10",
			"TransTimes=00:00;14:05",
			"TransInterval=1",
			"TransFlag=TransData AttLog",
			"Realtime=1",
			"Encrypt=None",
		]
	)


def receive_data(device, args, body: str) -> str:
	table = (args.get("table") or "").upper()

	if table == "ATTLOG":
		records = parse_attlog(body)
		result = ingest_records(device.name, records)
		values = {"last_sync_status": f"{now_datetime():%Y-%m-%d %H:%M:%S} push: {result['received']} received, {result['new']} new"}
		if args.get("Stamp"):
			values["attlog_stamp"] = args.get("Stamp")
		frappe.db.set_value(DEVICE_DOCTYPE, device.name, values, update_modified=False)
		return f"OK: {len(records)}"

	if table == "OPTIONS" and body:
		frappe.db.set_value(DEVICE_DOCTYPE, device.name, "device_info", body[:2000], update_modified=False)

	# OPERLOG, ATTPHOTO, USERINFO, BIODATA ... are acknowledged but not needed for attendance.
	return "OK"


def parse_attlog(body: str) -> list[dict]:
	records = []
	for line in (body or "").splitlines():
		m = ATTLOG_LINE.match(line)
		if not m:
			continue
		ts = m.group("ts").replace("T", " ")
		if len(ts) == 16:
			ts += ":00"
		records.append(
			{
				"user_id": m.group("pin"),
				"timestamp": ts,
				"punch_status": cint(m.group("state")),
				"verify_type": cint(m.group("verify")),
			}
		)
	return records


def pop_command(device) -> str:
	if not device.pending_command:
		return "OK"
	cmd_id = cint(device.command_id) + 1
	frappe.db.set_value(
		DEVICE_DOCTYPE,
		device.name,
		{"pending_command": None, "command_id": cmd_id},
		update_modified=False,
	)
	return f"C:{cmd_id}:{device.pending_command}\n"


def format_info(info: str) -> str:
	# INFO=Ver,UserCount,FPCount,AttCount,IP,FPVersion,FaceVersion,FaceCount,...
	labels = ["Firmware", "Users", "Fingerprints", "Attendance Records", "Device IP", "FP Algorithm", "Face Algorithm", "Face Templates"]
	parts = info.split(",")
	lines = [f"{label}: {value}" for label, value in zip(labels, parts) if value]
	return "\n".join(lines) or info[:2000]


def remember_unknown_device(sn: str, ip: str, endpoint: str):
	if not re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", sn):
		return
	unknown = frappe.cache.get_value(UNKNOWN_DEVICES_CACHE_KEY) or {}
	if sn not in unknown and len(unknown) >= 50:
		return
	unknown[sn] = {"ip": ip, "last_seen": str(now_datetime()), "endpoint": endpoint}
	frappe.cache.set_value(UNKNOWN_DEVICES_CACHE_KEY, unknown, expires_in_sec=7 * 24 * 3600)
