"""Per-request caches stored on frappe.local (a werkzeug Local, reset for every request/job)."""

import frappe


def request_cache(name: str) -> dict:
	cache = getattr(frappe.local, name, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, name, cache)
	return cache


def clear(*names: str):
	for name in names:
		setattr(frappe.local, name, {})
