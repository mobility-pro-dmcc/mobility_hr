// Copyright (c) 2026, Mobility Pro and contributors
// For license information, please see license.txt

const PUSH = "Push (ADMS / Cloud Server)";
const PULL = "Pull (TCP/IP)";

frappe.ui.form.on("Biometric Device", {
	onload(frm) {
		// Result of a background "Sync Now" job
		frappe.realtime.off("biometric_sync_done");
		frappe.realtime.on("biometric_sync_done", (data) => {
			if (data.success && data.message && data.message.includes("No employee")) {
				frappe.msgprint({ title: __("Punches processed"), message: data.message, indicator: "orange" });
			} else {
				frappe.show_alert({ message: data.message, indicator: data.success ? "green" : "red" }, 10);
			}
			if (cur_frm && cur_frm.doctype === "Biometric Device" && cur_frm.doc.name === data.device) {
				cur_frm.reload_doc();
			}
		});
	},

	refresh(frm) {
		frm.trigger("render_push_help");
		frm.trigger("show_status");

		if (frm.is_new()) return;

		frm.add_custom_button(__("Punch Logs"), () => {
			frappe.set_route("List", "Biometric Attendance Log", { device: frm.doc.name });
		});
		frm.add_custom_button(__("Sync Schedules"), () => frappe.set_route("Form", "Biometric Settings"));

		if (frm.doc.connection_type === PULL) {
			frm.add_custom_button(__("Sync Now"), () => start_sync(frm, 0), __("Actions"));
			frm.add_custom_button(
				__("Full Sync Now"),
				() =>
					frappe.confirm(
						__("Re-read every punch stored on the device? This can take several minutes. Already imported punches are skipped."),
						() => start_sync(frm, 1)
					),
				__("Actions")
			);
			frm.add_custom_button(
				__("Test Connection"),
				() =>
					frm
						.call({ doc: frm.doc, method: "test_connection", freeze: true, freeze_message: __("Connecting...") })
						.then((r) => {
							const rows = Object.entries(r.message || {})
								.map(([k, v]) => `<tr><td>${k}</td><td><b>${frappe.utils.escape_html(String(v))}</b></td></tr>`)
								.join("");
							frappe.msgprint({ title: __("Connected"), indicator: "green", message: `<table class="table table-bordered">${rows}</table>` });
							frm.reload_doc();
						}),
				__("Actions")
			);
		} else {
			frm.add_custom_button(
				__("Re-upload All Logs From Device"),
				() =>
					frappe.confirm(
						__("Ask the device to send all stored punches again? Punches already imported are skipped."),
						() => call(frm, "request_full_upload")
					),
				__("Actions")
			);
		}

		frm.add_custom_button(
			__("Retry Unprocessed Punches"),
			() =>
				frm.call({ doc: frm.doc, method: "reprocess_logs" }).then((r) => {
					frappe.show_alert({ message: r.message, indicator: "blue" }, 7);
				}),
			__("Actions")
		);
	},

	connection_type(frm) {
		frm.trigger("render_push_help");
	},

	show_status(frm) {
		if (frm.is_new()) return;
		frm.call({ doc: frm.doc, method: "get_summary" }).then((r) => {
			const s = r.message || {};
			frm.dashboard.clear_headline();
			const notFound = s["Employee Not Found"] || 0;
			const failed = s["Failed"] || 0;
			const ok = (s["Processed"] || 0) + (s["Duplicate"] || 0);
			let msg = __("{0} punches imported as check-ins", [ok]);
			if (s["Pending"]) msg += ` · ${__("{0} pending", [s["Pending"]])}`;
			if (notFound) msg += ` · <a href="/app/biometric-attendance-log?device=${encodeURIComponent(frm.doc.name)}&status=Employee%20Not%20Found">${__("{0} with no matching Employee (set Attendance Device ID)", [notFound])}</a>`;
			if (failed) msg += ` · <a href="/app/biometric-attendance-log?device=${encodeURIComponent(frm.doc.name)}&status=Failed">${__("{0} failed", [failed])}</a>`;
			frm.dashboard.set_headline(msg, notFound || failed ? "orange" : "green");
		});
	},

	render_push_help(frm) {
		const field = frm.get_field("push_setup_help");
		if (!field || frm.doc.connection_type !== PUSH) return;

		frappe
			.call("mobility_hr.mobility.doctype.biometric_device.biometric_device.get_push_settings")
			.then((r) => {
				const s = r.message;
				const unknown = Object.entries(s.unregistered_devices || {});
				let unknownHtml = "";
				if (unknown.length) {
					unknownHtml = `
						<p style="margin-top:12px"><b>${__("Devices that tried to connect but are not registered yet:")}</b></p>
						<ul>${unknown
							.map(
								([sn, d]) =>
									`<li><a href="#" class="use-sn" data-sn="${frappe.utils.escape_html(sn)}">${frappe.utils.escape_html(sn)}</a>
									<span class="text-muted">(${frappe.utils.escape_html(d.ip || "")}, ${frappe.utils.escape_html(d.last_seen || "")})</span></li>`
							)
							.join("")}</ul>`;
				}
				const seen = frm.doc.last_activity
					? `<span class="indicator-pill green">${__("Last contact")}: ${frappe.datetime.prettyDate(frm.doc.last_activity)}</span>`
					: frm.is_new()
					? ""
					: `<span class="indicator-pill orange">${__("Device has not connected yet")}</span>`;

				field.$wrapper.html(`
					<div class="alert alert-info" style="margin-top:8px">
						${seen}
						<p style="margin-top:8px"><b>${__("On the device")}:</b> ${__("Menu › Comm. › Cloud Server Setting (or ADMS)")}</p>
						<table class="table table-sm table-bordered" style="background:var(--card-bg);max-width:520px">
							<tr><td>${__("Enable Domain Name")}</td><td><b>${__("ON if the address below is a domain")}</b></td></tr>
							<tr><td>${__("Server Address")}</td><td><b>${frappe.utils.escape_html(s.server_address)}</b></td></tr>
							<tr><td>${__("Server Port")}</td><td><b>${s.port}</b></td></tr>
							<tr><td>${__("HTTPS")}</td><td><b>${s.https ? __("ON (only if your model supports it)") : __("OFF")}</b></td></tr>
							<tr><td>${__("Enable Proxy Server")}</td><td><b>OFF</b></td></tr>
						</table>
						<p class="text-muted">${__("Save this device with the same Serial Number shown on the device. Then set the Attendance Device ID on each Employee to the user's ID on the device.")}</p>
						${unknownHtml}
					</div>`);

				field.$wrapper.find(".use-sn").on("click", (e) => {
					e.preventDefault();
					frm.set_value("serial_number", $(e.currentTarget).data("sn"));
				});
			});
	},
});

function start_sync(frm, full) {
	return frm.call({ doc: frm.doc, method: "sync_now", args: { full } }).then((r) => {
		const res = r.message || {};
		frappe.show_alert({ message: res.message, indicator: res.started ? "blue" : "orange" }, 7);
	});
}

function call(frm, method, freeze_message) {
	return frm.call({ doc: frm.doc, method, freeze: true, freeze_message }).then((r) => {
		if (r.message) frappe.msgprint(r.message);
		frm.reload_doc();
	});
}
