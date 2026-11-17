from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in mobility_hr/__init__.py
from mobility_hr import __version__ as version

setup(
	name="mobility_hr",
	version=version,
	description="Custom App for Mobilty",
	author="Frappe",
	author_email="developers@frappe.io",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
