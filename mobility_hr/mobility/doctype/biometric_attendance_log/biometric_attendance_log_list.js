frappe.listview_settings["Biometric Attendance Log"] = {
	get_indicator(doc) {
		const colors = {
			Pending: "orange",
			Processed: "green",
			Duplicate: "blue",
			"Employee Not Found": "red",
			Skipped: "gray",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
