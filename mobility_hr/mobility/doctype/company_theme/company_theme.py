# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class CompanyTheme(Document):
	def validate(self):
		for field in ("accent_color", "bright_accent", "bright_background", "bright_surface", "dark_background", "dark_surface"):
			value = self.get(field)
			if value and not HEX.match(value):
				frappe.throw(_("{0} must be a colour like #29f2e8.").format(self.meta.get_label(field)))

	def on_update(self):
		frappe.clear_cache()
