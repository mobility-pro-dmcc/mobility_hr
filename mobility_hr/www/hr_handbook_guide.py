import frappe
from frappe import _

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/hr-handbook-guide"
		raise frappe.Redirect
	context.title = _("HR Handbook Guide")
	context.no_breadcrumbs = True
	context.img = "/assets/mobility_hr/images/hr_handbook_guide/"
	context.img_version = _images_version()
	context.handbook = frappe.db.get_value(
		"HR Handbook", {"is_active": 1}, ["name", "company", "effective_from"], as_dict=True, order_by="priority desc"
	)
	return context


def _images_version() -> int:
	"""Newest modification time of the guide's screenshots. Added to image URLs so browsers
	fetch new pictures as soon as they are replaced (nginx caches /assets for a year)."""
	import os

	folder = frappe.get_app_path("mobility_hr", "public", "images", "hr_handbook_guide")
	try:
		return int(max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder)))
	except (OSError, ValueError):
		return 0
