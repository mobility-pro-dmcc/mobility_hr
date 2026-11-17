# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class BiometricSettings(Document):
	def validate(self):
		if self.enable_interval_sync and cint(self.sync_interval_minutes) < 1:
			frappe.throw(_("Interval must be at least 1 minute."))

		seen = set()
		for row in self.schedules:
			key = (str(row.sync_time), row.device or "")
			if key in seen:
				frappe.throw(_("Row {0}: duplicate schedule for the same time and device.").format(row.idx))
			seen.add(key)
