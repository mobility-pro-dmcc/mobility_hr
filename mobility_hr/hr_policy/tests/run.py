"""Run mobility_hr.hr_policy tests against a site WITHOUT persisting anything.

frappe.db.commit is disabled for the whole run and everything is rolled back at the end.
Usage: env/bin/python run_hr_tests.py <site> [module ...]
"""

import sys
import unittest

import frappe

site = sys.argv[1]
modules = sys.argv[2:] or ["mobility_hr.hr_policy.tests.test_logic", "mobility_hr.hr_policy.tests.test_integration"]

frappe.init(site=site, sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")
frappe.flags.in_test = True
frappe.flags.mute_emails = True

_commit = frappe.db.commit
frappe.db.commit = lambda *a, **k: None  # nothing may persist
try:
	suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(m) for m in modules)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
finally:
	frappe.db.rollback()
	frappe.db.commit = _commit
	frappe.destroy()
sys.exit(0 if result.wasSuccessful() else 1)
