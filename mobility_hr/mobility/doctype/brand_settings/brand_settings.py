# Copyright (c) 2026, Mobility Pro and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BrandSettings(Document):
	def on_update(self):
		from mobility_hr.branding import apply_branding

		apply_branding()
		frappe.clear_cache()
