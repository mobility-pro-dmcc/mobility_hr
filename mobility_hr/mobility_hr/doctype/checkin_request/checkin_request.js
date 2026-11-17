// Copyright (c) 2026, Mobility Pro and contributors
// For license information, please see license.txt

frappe.ui.form.on("Checkin Request", {
	setup(frm) {
		// Prefill from the dashboard link: /app/checkin-request/new?date=YYYY-MM-DD
		const params = new URLSearchParams(window.location.search);
		if (frm.is_new() && params.get("date") && !frm.doc.date) frm.set_value("date", params.get("date"));
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.employee) {
			frappe.db.get_value("Employee", { user_id: frappe.session.user, status: "Active" }, "name").then((r) => {
				if (r.message && r.message.name) frm.set_value("employee", r.message.name);
			});
		}
	},

	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.employee_checkin) {
			frm.dashboard.set_headline(
				__("Check-in {0} was added.", [`<a href="/app/employee-checkin/${frm.doc.employee_checkin}">${frm.doc.employee_checkin}</a>`]) +
					(frm.doc.result_note ? " " + frappe.utils.escape_html(frm.doc.result_note) : ""),
				"green"
			);
		}
	},
});
