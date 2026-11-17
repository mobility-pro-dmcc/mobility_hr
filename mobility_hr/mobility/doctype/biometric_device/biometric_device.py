# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from mobility_hr.biometric.processing import (
	LOG_DOCTYPE,
	PULL,
	PUSH,
	enqueue_processing,
	process_pending_logs,
)


class BiometricDevice(Document):
	def validate(self):
		if self.serial_number:
			self.serial_number = self.serial_number.strip()
		if self.connection_type == PUSH and not self.serial_number:
			frappe.throw(_("Serial Number is required for Push devices."))
		if self.connection_type == PULL and not self.ip_address:
			frappe.throw(_("IP Address is required for Pull devices."))
		if self.has_value_changed("serial_number") and not self.is_new():
			self.attlog_stamp = None

	def after_insert(self):
		from mobility_hr.biometric.adms import UNKNOWN_DEVICES_CACHE_KEY

		unknown = frappe.cache.get_value(UNKNOWN_DEVICES_CACHE_KEY) or {}
		if self.serial_number in unknown:
			unknown.pop(self.serial_number)
			frappe.cache.set_value(UNKNOWN_DEVICES_CACHE_KEY, unknown, expires_in_sec=7 * 24 * 3600)

	def on_update(self):
		if self.has_value_changed("import_logs_from") or self.has_value_changed("time_offset_minutes"):
			frappe.clear_document_cache(self.doctype, self.name)

		if self.has_value_changed("import_logs_from") and not self.is_new():
			# Re-evaluate punches skipped under the old cut-off date.
			frappe.db.set_value(
				LOG_DOCTYPE,
				{"device": self.name, "status": "Skipped"},
				{"status": "Pending", "error": None},
				update_modified=False,
			)
			enqueue_processing(self.name)

	@frappe.whitelist()
	def sync_now(self, full=0):
		"""Runs in a background job; the form is notified over realtime when it finishes."""
		frappe.only_for(("HR Manager", "System Manager"))
		if self.connection_type == PULL:
			job = frappe.enqueue(
				"mobility_hr.biometric.pull.sync_device_job",
				queue="long",
				timeout=1800,
				device_name=self.name,
				full=frappe.utils.cint(full),
				user=frappe.session.user,
				job_id=f"biometric_pull::{self.name}",
				deduplicate=True,
			)
			if not job:
				return {"started": False, "message": _("A sync for this device is already running. You will see the result here when it finishes.")}
			return {"started": True, "message": _("Sync started in the background. You will be notified when it finishes.")}

		enqueue_processing(self.name)
		return {"started": False, "message": _("Push devices send punches automatically. Pending punches are being processed.")}

	@frappe.whitelist()
	def test_connection(self):
		frappe.only_for(("HR Manager", "System Manager"))
		from mobility_hr.biometric.pull import test_connection

		return test_connection(self.name)

	@frappe.whitelist()
	def reprocess_logs(self, include_failed=1):
		frappe.only_for(("HR Manager", "System Manager"))
		statuses = ["Pending", "Employee Not Found"]
		if frappe.utils.cint(include_failed):
			statuses.append("Failed")
		job = frappe.enqueue(
			"mobility_hr.biometric.processing.reprocess_device_job",
			queue="long",
			timeout=3600,
			device=self.name,
			statuses=statuses,
			user=frappe.session.user,
			job_id=f"biometric_reprocess::{self.name}",
			deduplicate=True,
		)
		if not job:
			return _("Already running for this device. You will be notified when it finishes.")
		return _("Processing started in the background. You will be notified when it finishes.")

	@frappe.whitelist()
	def request_full_upload(self):
		"""Push devices: forget the upload stamp and ask the device to re-send every stored punch."""
		frappe.only_for(("HR Manager", "System Manager"))
		if self.connection_type != PUSH:
			frappe.throw(_("Only available for Push devices."))
		self.db_set({"attlog_stamp": None, "pending_command": "CHECK"}, update_modified=False)
		return _("The device will re-upload its logs within a minute. Already imported punches are skipped.")

	@frappe.whitelist()
	def get_summary(self):
		rows = frappe.get_all(
			LOG_DOCTYPE,
			filters={"device": self.name},
			fields=["status", "count(name) as count"],
			group_by="status",
		)
		return {r.status: r.count for r in rows}


@frappe.whitelist()
def get_push_settings():
	"""Values the user types into the device's Cloud Server Setting."""
	frappe.only_for(("HR Manager", "System Manager"))
	from urllib.parse import urlparse

	from mobility_hr.biometric.adms import UNKNOWN_DEVICES_CACHE_KEY

	url = urlparse(get_url())
	port = url.port or (443 if url.scheme == "https" else 80)
	return {
		"server_address": url.hostname,
		"port": port,
		"https": url.scheme == "https",
		"endpoint": f"{get_url()}/iclock/cdata",
		"unregistered_devices": frappe.cache.get_value(UNKNOWN_DEVICES_CACHE_KEY) or {},
	}
