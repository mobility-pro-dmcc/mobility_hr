// Website pages (see mobility_hr/branding.py). On the login / password pages adds two toggles:
// - Bright / Dark: saved in this browser (localStorage "mp_theme"); desk, website and portal read it.
// - English / العربية: saved as the `preferred_language` cookie (and the user's language once logged in).
(function () {
	const KEY = "mp_theme";
	const root = document.documentElement;
	const SUN =
		'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
	const MOON =
		'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

	function mode() {
		return root.getAttribute("data-mp-mode") === "dark" ? "dark" : "bright";
	}

	function syncLogo() {
		// Frappe's login logo: Company Theme › Logo for Bright / Dark Mode (see branding.website_context)
		const L = window.mp_logos || {};
		const src = mode() === "dark" ? L.dark || L.bright : L.bright || L.dark;
		if (src) document.querySelectorAll("img.app-logo").forEach((img) => img.getAttribute("src") !== src && img.setAttribute("src", src));
	}

	function setMode(m) {
		root.setAttribute("data-mp-mode", m);
		syncLogo();
		try {
			localStorage.setItem(KEY, m);
		} catch (e) {}
	}

	function setLanguage(lang) {
		const body = new URLSearchParams({ lang });
		fetch("/api/method/mobility_hr.branding.set_language", {
			method: "POST",
			headers: { "X-Frappe-CSRF-Token": (window.frappe && frappe.csrf_token) || "", "Content-Type": "application/x-www-form-urlencoded" },
			body,
		}).finally(() => location.reload());
	}

	function render() {
		if (!document.body || !document.body.classList.contains("mp-brand")) return;
		syncLogo();
		const path = document.body.getAttribute("data-path");
		if (path !== "login" && path !== "update-password") return;
		const isAr = (root.getAttribute("lang") || "").startsWith("ar") || root.getAttribute("dir") === "rtl";
		const box = document.createElement("div");
		box.className = "mp-toggles";
		box.innerHTML =
			'<button type="button" class="mp-mode-btn"></button>' +
			'<button type="button" class="mp-lang-btn" lang="' + (isAr ? "en" : "ar") + '">' + (isAr ? "English" : "العربية") + "</button>";
		const modeBtn = box.querySelector(".mp-mode-btn");
		const paint = () => {
			const dark = mode() === "dark";
			// show the mode you switch TO
			modeBtn.innerHTML = dark ? SUN : MOON;
			modeBtn.title = dark ? (isAr ? "الوضع الفاتح" : "Bright mode") : isAr ? "الوضع الداكن" : "Dark mode";
			modeBtn.setAttribute("aria-label", modeBtn.title);
		};
		modeBtn.addEventListener("click", () => {
			setMode(mode() === "dark" ? "bright" : "dark");
			paint();
		});
		box.querySelector(".mp-lang-btn").addEventListener("click", () => setLanguage(isAr ? "en" : "ar"));
		paint();
		document.body.appendChild(box);
	}

	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render);
	else render();
})();
