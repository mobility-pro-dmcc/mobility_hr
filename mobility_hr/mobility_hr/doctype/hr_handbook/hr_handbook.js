// Copyright (c) 2026, Mobility Pro and contributors
// For license information, please see license.txt

const DEFAULT_GUIDE = "/hr-handbook-guide";

frappe.ui.form.on("HR Handbook", {
	refresh(frm) {
		// Blue "HR Handbook Guide" link in the left sidebar, opening the guide page inside the app.
		if (!frm.sidebar || frm.sidebar.user_actions.find(".hr-handbook-guide-link").length) return;
		const url = frm.doc.documentation_url || DEFAULT_GUIDE;
		frm.sidebar
			.add_user_action(
				`${frappe.utils.icon("es-line-book", "sm")} <span>${__("HR Handbook Guide")}</span>`,
				() => window.open(url, "_blank", "noopener")
			)
			.addClass("hr-handbook-guide-link")
			.attr("href", url)
			.css({ color: "var(--blue-500, #2490ef)", "font-weight": 600, display: "inline-flex", gap: "6px", "align-items": "center" });
	},
});
