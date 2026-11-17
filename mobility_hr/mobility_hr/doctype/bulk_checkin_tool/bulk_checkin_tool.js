// Copyright (c) 2026, Mobility Pro and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bulk Checkin Tool", {
	onload(frm) {
		frappe.realtime.off("bulk_checkin_done");
		frappe.realtime.on("bulk_checkin_done", (summary) => show_result(frm, summary));
	},

	refresh(frm) {
		frm.disable_save();
		frm.page.set_primary_action(__("Create Check-ins"), () => create(frm));
		frm.add_custom_button(__("Add Employees from Filters"), () => add_from_filters(frm));
		frm.add_custom_button(__("Clear Employees"), () => frm.clear_table("employees") || frm.refresh_field("employees"));
		frm.add_custom_button(__("Check-in Requests"), () =>
			frappe.set_route("List", "Checkin Request", { source: "HR Bulk Entry" })
		);
		if (!frm.doc.date) frm.set_value("date", frappe.datetime.get_today());
		frm.set_query("employees", () => ({ filters: { status: "Active" } }));
	},
});

function add_from_filters(frm) {
	frm.call({ doc: frm.doc, method: "get_employees" }).then((r) => {
		const have = new Set((frm.doc.employees || []).map((d) => d.employee));
		let added = 0;
		(r.message || []).forEach((e) => {
			if (have.has(e.name)) return;
			frm.add_child("employees", { employee: e.name });
			added++;
		});
		frm.refresh_field("employees");
		frappe.show_alert({ message: __("{0} employee(s) added", [added]), indicator: "green" });
	});
}

function create(frm) {
	const count = (frm.doc.employees || []).length;
	if (!count) return frappe.msgprint(__("Select at least one employee."));
	if (!frm.doc.date || !frm.doc.time || !frm.doc.reason) return frappe.msgprint(__("Date, Time and Reason are required."));
	frappe.confirm(
		__("Create a check-in at {0} {1} for {2} employee(s)? Each one is approved immediately and that day's attendance is recalculated.", [
			frappe.datetime.str_to_user(frm.doc.date), frm.doc.time, count,
		]),
		() =>
			frm.call({ doc: frm.doc, method: "create_checkins", freeze: true, freeze_message: __("Creating check-ins...") }).then((r) => {
				const res = r.message || {};
				if (res.queued) {
					frappe.show_alert({ message: __("{0} check-ins are being created in the background. Results will appear here.", [res.count]), indicator: "blue" }, 8);
				} else {
					show_result(frm, res);
				}
			})
	);
}

function show_result(frm, summary) {
	const rows = (summary.results || [])
		.map((r) => `<tr>
			<td>${frappe.utils.escape_html(r.employee)}</td>
			${r.ok
				? `<td><span class="indicator-pill green">${__("Created")}</span> <a href="/app/checkin-request/${r.request}">${r.request}</a></td><td class="text-muted">${frappe.utils.escape_html(r.note || "")}</td>`
				: `<td><span class="indicator-pill red">${__("Skipped")}</span></td><td>${frappe.utils.escape_html(r.error || "")}</td>`}
		</tr>`)
		.join("");
	frm.get_field("last_result").$wrapper.html(`
		<p><b>${__("{0} created, {1} skipped", [summary.created || 0, summary.failed || 0])}</b></p>
		<table class="table table-bordered table-sm"><thead><tr><th>${__("Employee")}</th><th>${__("Result")}</th><th>${__("Details")}</th></tr></thead><tbody>${rows}</tbody></table>`);
	frappe.show_alert({ message: __("{0} check-in(s) created, {1} skipped", [summary.created || 0, summary.failed || 0]), indicator: summary.failed ? "orange" : "green" }, 8);
	if (summary.created) {
		frm.clear_table("employees");
		frm.refresh_field("employees");
	}
}
