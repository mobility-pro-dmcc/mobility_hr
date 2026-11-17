"""Brand design for the desk, login page, website and employee portal.

Configured in data, never hard-coded:
- Brand Settings: on/off, the company shown on the login page and website, tagline, fallback logo,
  default accent.
- Company Theme (one per company): accent colour and the colours of Bright and Dark mode,
  optional display name.
- Company › Company Logo: the logo (Brand Settings › Fallback Logo if a company has none).

Mode: Bright (default) or Dark, chosen per browser with the toggle on the login page and stored in
localStorage["mp_theme"]. The desk, website, login page and portal all read the same value.

Which company's theme is used:
- desk → the logged-in user's own company (Employee.company), else the login/website company;
- website → the logged-in user's own company; guests and the login page → Brand Settings ›
  Login & Website Company (default: the default company);
- employee portal → the company being viewed (HR can switch between permitted companies).

Every style is scoped to the `mp-brand` class, added only when Brand Settings › Enable is ticked.
"""

import json

import frappe
from frappe.utils import escape_html
from frappe.utils.jinja_globals import is_rtl

SETTINGS = "Brand Settings"
THEME = "Company Theme"
DEFAULT_ACCENT = "#29f2e8"
DEFAULT_TAGLINE = "Employee portal & ERP"
MODE_KEY = "mp_theme"  # localStorage key: "bright" | "dark"
BRIGHT_DEFAULT_KEY = "mobility_brand_bright_default_applied"
LEGACY_BRAND_HTML_MARK = 'class="mp-brand-mark"'
DEFAULT_THEME = {
	"bright_background": "#ffffff",
	"bright_surface": "#f6f7f8",
	"dark_background": "#000000",
	"dark_surface": "#0b0b0b",
}


def settings():
	if not frappe.db.exists("DocType", SETTINGS):
		return frappe._dict()
	return frappe.get_cached_doc(SETTINGS)


def enabled() -> bool:
	return bool(settings().get("enabled"))


def default_company() -> str | None:
	return settings().get("brand_company") or frappe.db.get_single_value("Global Defaults", "default_company")


def user_company(user: str | None = None) -> str | None:
	"""Company of the user's Employee record (None for Guest / users without one)."""
	user = user or frappe.session.user
	if user == "Guest":
		return None
	return frappe.db.get_value("Employee", {"user_id": user, "status": ["!=", "Left"]}, "company")


def company_theme(company: str | None) -> dict:
	"""Colours for a company: its Company Theme, else defaults with the Brand Settings accent."""
	row = None
	if company and frappe.db.exists("DocType", THEME) and frappe.db.exists(THEME, {"company": company, "enabled": 1}):
		row = frappe.get_cached_doc(THEME, company)
	theme = dict(DEFAULT_THEME)
	theme["accent"] = (row.accent_color if row else None) or settings().get("accent_color") or DEFAULT_ACCENT
	theme["bright_accent"] = row.bright_accent if row and row.bright_accent else None
	if row:
		for key in DEFAULT_THEME:
			if row.get(key):
				theme[key] = row.get(key)
	theme["display_name"] = row.display_name if row else None
	theme["bright_logo"] = row.bright_logo if row else None
	theme["dark_logo"] = row.dark_logo if row else None
	return theme


def company_brand(company: str | None = None) -> dict:
	"""{"company", "name", "logo", "logo_bright", "logo_html", "initials", "theme"} for a company
	(default: the login/website company).

	Logos: `logo` is the Dark Mode logo (Company Theme › Logo for Dark Mode, else Company Logo, else
	Brand Settings › Fallback Logo). `logo_bright` is Company Theme › Logo for Bright Mode, or None,
	in which case Bright Mode shows `logo` turned black (CSS class mp-logo-auto)."""
	company = company or default_company()
	company_name, company_logo = (
		frappe.get_cached_value("Company", company, ["company_name", "company_logo"])
		if company and frappe.db.exists("Company", company)
		else (None, None)
	)
	theme = company_theme(company)
	name = theme.pop("display_name") or company_name or company or frappe.get_website_settings("app_name") or "ERP"
	logo_bright = theme.pop("bright_logo")
	logo = theme.pop("dark_logo") or company_logo or settings().get("logo")
	initials = "".join(w[0] for w in name.split()[:2]).upper()
	return {
		"company": company,
		"name": name,
		"logo": logo,
		"logo_bright": logo_bright,
		"logo_html": logo_html(logo, logo_bright),
		"initials": initials,
		"theme": theme,
	}


def logo_html(logo: str | None, logo_bright: str | None) -> str:
	"""<img> markup that shows the right logo for the mode (CSS: .mp-logo-dark / .mp-logo-bright / .mp-logo-auto)."""
	if not logo and not logo_bright:
		return ""
	if logo_bright and logo:
		return (
			f'<img class="mp-logo mp-logo-dark" src="{escape_html(logo)}" alt="">'
			f'<img class="mp-logo mp-logo-bright" src="{escape_html(logo_bright)}" alt="">'
		)
	if logo_bright:
		return f'<img class="mp-logo mp-logo-bright-only" src="{escape_html(logo_bright)}" alt="">'
	return f'<img class="mp-logo mp-logo-auto" src="{escape_html(logo)}" alt="">'


def theme_css_vars(theme: dict) -> str:
	"""CSS custom properties read by all brand stylesheets."""
	tagline = json.dumps(frappe._(settings().get("tagline") or DEFAULT_TAGLINE), ensure_ascii=False).replace("<", "\\3c ")
	parts = {
		"--mp-accent": theme["accent"],
		"--accent": theme["accent"],
		"--mp-dark-bg": theme["dark_background"],
		"--mp-dark-surface": theme["dark_surface"],
		"--mp-bright-bg": theme["bright_background"],
		"--mp-bright-surface": theme["bright_surface"],
	}
	if theme.get("bright_accent"):
		parts["--mp-bright-accent"] = theme["bright_accent"]
	css = "; ".join(f"{k}: {escape_html(v)}" for k, v in parts.items())
	# login heading: the same for every company (Frappe's own says "Login to <company>")
	login_title = json.dumps(frappe._("Welcome back"), ensure_ascii=False).replace("<", "\\3c ")
	return f"{css}; --mp-tagline: {tagline}; --mp-login-title: {login_title};"


def mode_script() -> str:
	"""Runs in <head> before paint: applies the saved mode (bright by default) and, for Arabic, RTL."""
	rtl = "true" if is_rtl() else "false"
	return (
		"<script>(function(){var m;try{m=localStorage.getItem('" + MODE_KEY + "')}catch(e){}"
		"var r=document.documentElement;r.setAttribute('data-mp-mode',m==='dark'?'dark':'bright');"
		"if(" + rtl + "){r.setAttribute('dir','rtl')}})();</script>"
	)


def style_tag(company: str | None = None) -> str:
	return f"<style>html.mp-brand, body.mp-brand {{ {theme_css_vars(company_brand(company)['theme'])} }}</style>"


# --------------------------------------------------------------------------- hooks


def boot_session(bootinfo):
	"""extend_bootinfo: desk JS applies the theme of the user's own company and the saved mode."""
	if not enabled():
		bootinfo.mobility_brand = 0
		return
	brand = company_brand(user_company())
	bootinfo.mobility_brand = {
		"company": brand["company"],
		"name": brand["name"],
		"theme": brand["theme"],
		"mode_key": MODE_KEY,
		"logo": brand["logo"],
		"logo_bright": brand["logo_bright"],
	}
	if brand["logo_bright"] or brand["logo"]:
		# desk navbar logo; Bright is the default mode, mobility_brand.js swaps it for Dark
		bootinfo.app_logo_url = brand["logo_bright"] or brand["logo"]


def skip_page_cache_for_users():
	"""before_request: Frappe caches website pages by path + language only. Logged-in users see their own
	company's brand, so their pages must not be read from (or written to) that shared cache."""
	if frappe.session and frappe.session.user != "Guest" and enabled():
		frappe.local.no_cache = 1


def website_context(context):
	"""update_website_context: runs after each page's own get_context, so it can override what the
	login page and website navbar show (name, logo) for this request only."""
	if not enabled():
		return context
	# logged-in employees see their own company; guests (login page) the Login & Website Company
	company = user_company()
	brand = company_brand(company)
	context.body_class = f"{context.get('body_class') or ''} mp-brand".strip()
	context.brand_html = f'<span class="mp-brand-mark">{brand["logo_html"]}<b>{escape_html(brand["name"]).upper()}</b></span>'
	context.app_name = brand["name"]  # login heading: "Login to <company>"
	if brand["logo_bright"] or brand["logo"]:
		context.logo = brand["logo_bright"] or brand["logo"]  # login page logo (mobility_website.js swaps it per mode)
	logos = json.dumps({"dark": brand["logo"], "bright": brand["logo_bright"]}).replace("<", "\\u003c")
	# printed by templates/includes/head.html on every website page
	context.head_html = (
		(context.get("head_html") or "") + mode_script() + style_tag(company) + f"<script>window.mp_logos={logos};"
		+ ("document.documentElement.classList.add('mp-has-bright-logo');" if brand["logo_bright"] else "")
		+ "</script>"
	)
	return context


def apply_branding():
	"""after_migrate / Brand Settings save. Only stores what Frappe needs outside page rendering."""
	if not frappe.db.exists("DocType", SETTINGS):
		return
	_migrate_site_config_flag()
	_remove_legacy_values()
	if not enabled():
		return
	brand = company_brand()
	if brand["logo"] and not frappe.db.get_single_value("Website Settings", "favicon"):
		frappe.db.set_single_value("Website Settings", "favicon", brand["logo"])
	_bright_by_default()
	frappe.clear_cache()


def _bright_by_default():
	"""Bright is the default mode. Undo the earlier 'everyone Dark' default once."""
	frappe.db.delete("Property Setter", {"doc_type": "User", "field_name": "desk_theme", "property": "default", "value": "Dark"})
	if not frappe.db.get_default(BRIGHT_DEFAULT_KEY):
		frappe.db.set_value("User", {"user_type": "System User", "desk_theme": "Dark"}, "desk_theme", "Light", update_modified=False)
		frappe.db.set_default(BRIGHT_DEFAULT_KEY, "1")


def _migrate_site_config_flag():
	"""Earlier versions were switched on with `mobility_brand` in site_config: carry that over once."""
	if frappe.conf.get("mobility_brand") and not frappe.db.get_single_value(SETTINGS, "enabled") and not frappe.db.get_default("mobility_brand_settings_migrated"):
		doc = frappe.get_single(SETTINGS)
		doc.enabled = 1
		doc.brand_company = doc.brand_company or frappe.db.get_single_value("Global Defaults", "default_company")
		doc.flags.ignore_permissions = True
		doc.save()
		frappe.db.set_default("mobility_brand_settings_migrated", "1")


def _remove_legacy_values():
	"""Undo values older versions wrote (now provided per request / replaced by Company Theme)."""
	if LEGACY_BRAND_HTML_MARK in (frappe.db.get_single_value("Website Settings", "brand_html") or ""):
		frappe.db.set_single_value("Website Settings", "brand_html", None)
	if frappe.db.exists("DocType", "Brand Company"):  # display names moved to Company Theme
		frappe.delete_doc("DocType", "Brand Company", ignore_permissions=True, force=True)


# --------------------------------------------------------------------------- portal helpers


def permitted_companies() -> list[dict]:
	"""Companies the current user may read, each with its brand (name, logo, theme)."""
	rows = frappe.get_list("Company", fields=["name"], order_by="name asc", limit_page_length=500)
	return [company_brand(r.name) for r in rows]


@frappe.whitelist(allow_guest=True, methods=["POST"])
def set_language(lang: str):
	"""Language toggle (login page and portal): English / Arabic.
	Guests get the `preferred_language` cookie Frappe already reads; logged-in users have it saved."""
	if lang not in ("en", "ar"):
		frappe.throw("Unsupported language")
	frappe.local.cookie_manager.set_cookie("preferred_language", lang)
	if frappe.session.user != "Guest":
		frappe.db.set_value("User", frappe.session.user, "language", lang)
		frappe.clear_cache(user=frappe.session.user)
	return lang
