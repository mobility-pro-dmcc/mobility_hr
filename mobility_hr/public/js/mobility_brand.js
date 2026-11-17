// Brand for the desk (see mobility_hr/branding.py):
// - adds the `mp-brand` class and the company theme colours (Company Theme of the user's company);
// - applies the mode saved in this browser (Bright by default; set by the login-page toggle) by
//   switching Frappe's own light/dark theme;
// - when the user changes theme from Frappe's "Toggle Theme", the choice is saved for the
//   login page, website and portal too.
(function () {
	const VARS = {
		accent: ["--mp-accent"],
		bright_accent: ["--mp-bright-accent"],
		dark_background: ["--mp-dark-bg"],
		dark_surface: ["--mp-dark-surface"],
		bright_background: ["--mp-bright-bg"],
		bright_surface: ["--mp-bright-surface"],
	};
	const root = document.documentElement;
	let key = "mp_theme";

	function savedMode() {
		try {
			return localStorage.getItem(key) === "dark" ? "dark" : "bright";
		} catch (e) {
			return "bright";
		}
	}

	let logos = {};
	function logoFor(mode) {
		return mode === "dark" ? logos.dark || logos.bright : logos.bright || logos.dark;
	}
	function syncLogo(mode) {
		// navbar logo: Company Theme › Logo for Bright / Dark Mode (else the Company Logo)
		const src = logoFor(mode);
		if (!src) return;
		if (window.frappe && frappe.boot) frappe.boot.app_logo_url = src; // before the navbar is drawn
		document.querySelectorAll(".navbar-brand .app-logo").forEach((img) => {
			if (img.getAttribute("src") !== src) img.setAttribute("src", src);
		});
	}

	function applyMode(mode) {
		const frappeTheme = mode === "dark" ? "dark" : "light";
		root.setAttribute("data-mp-mode", mode);
		syncLogo(mode);
		if (root.getAttribute("data-theme") !== frappeTheme) root.setAttribute("data-theme", frappeTheme);
		if (root.getAttribute("data-theme-mode") !== frappeTheme) root.setAttribute("data-theme-mode", frappeTheme);
	}

	function apply() {
		const brand = window.frappe && frappe.boot && frappe.boot.mobility_brand;
		if (!brand) return false;
		key = brand.mode_key || key;
		root.classList.add("mp-brand");
		logos = { dark: brand.logo, bright: brand.logo_bright };
		root.classList.toggle("mp-has-bright-logo", !!brand.logo_bright);
		const theme = brand.theme || {};
		Object.keys(VARS).forEach((k) => {
			if (theme[k]) VARS[k].forEach((v) => root.style.setProperty(v, theme[k]));
		});
		applyMode(savedMode());
		return true;
	}

	function watchFrappeToggle() {
		// Frappe's theme switcher changes data-theme on <html>: remember it in this browser.
		new MutationObserver(() => {
			const t = root.getAttribute("data-theme");
			const mode = t === "dark" ? "dark" : "bright";
			if (root.getAttribute("data-mp-mode") !== mode) {
				root.setAttribute("data-mp-mode", mode);
				syncLogo(mode);
				try {
					localStorage.setItem(key, mode);
				} catch (e) {}
			}
		}).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
	}

	if (apply()) watchFrappeToggle();
	else
		document.addEventListener("DOMContentLoaded", () => {
			if (apply()) watchFrappeToggle();
		});
})();
