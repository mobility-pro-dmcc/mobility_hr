// Copyright (c) 2026, Mobility Pro and contributors
// For license information, please see license.txt

frappe.ui.form.on("Company Theme", {
	refresh(frm) {
		load_company_logo(frm);
	},
	company: load_company_logo,
	bright_logo: render_preview,
	dark_logo: render_preview,
	accent_color: render_preview,
	bright_accent: render_preview,
	bright_background: render_preview,
	bright_surface: render_preview,
	dark_background: render_preview,
	dark_surface: render_preview,
});

function ink(hex) {
	// same rule as the CSS: accent mixed 55% with black
	const n = parseInt((hex || "#29f2e8").slice(1), 16);
	const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => Math.round(v * 0.55));
	return "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
}

function load_company_logo(frm) {
	// the Company Logo is used when the theme has no logo of its own
	if (!frm.doc.company) return render_preview(frm);
	frappe.db.get_value("Company", frm.doc.company, "company_logo").then((r) => {
		frm._company_logo = (r.message && r.message.company_logo) || "";
		render_preview(frm);
	});
}

function render_preview(frm) {
	const d = frm.doc;
	const accent = d.accent_color || "#29f2e8";
	const brightInk = d.bright_accent || ink(accent);
	const name = frappe.utils.escape_html(d.display_name || d.company || __("Company"));
	const darkLogo = d.dark_logo || frm._company_logo || "";
	const brightLogo = d.bright_logo || darkLogo;
	const logo = (src, autoDark) =>
		src ? `<img src="${encodeURI(src)}" alt="" style="height:20px;width:auto;margin-inline-end:10px;vertical-align:middle;${autoDark ? "filter:brightness(0)" : ""}">` : "";
	const card = (bg, surface, fg, muted, hl, btnBg, btnFg, glow, logoHtml) => `
		<div style="flex:1;min-width:240px;border-radius:16px;overflow:hidden;border:1px solid rgba(127,127,127,.25);background:${bg};color:${fg};
			background-image:radial-gradient(ellipse 60% 70% at 50% -20%, ${glow}, transparent 70%)">
			<div style="padding:14px 16px;font-weight:600;letter-spacing:.2em;font-size:12px">${logoHtml}${name.toUpperCase()}</div>
			<div style="padding:4px 16px 18px">
				<div style="font-size:22px;font-weight:600">${__("Good morning,")} <i style="color:${hl};font-family:Georgia,serif">${__("Sara")}</i></div>
				<div style="margin:12px 0;padding:12px;border-radius:12px;background:${surface};border:1px solid rgba(127,127,127,.18)">
					<div style="font-size:12px;color:${muted}">${__("Days worked")}</div><div style="font-size:26px;font-weight:600;color:${hl}">18</div></div>
				<span style="display:inline-block;padding:8px 16px;border-radius:999px;background:${btnBg};color:${btnFg};font-weight:600;font-size:13px">${__("Request leave")}</span>
			</div>
		</div>`;
	frm.get_field("preview").$wrapper.html(`<div style="display:flex;gap:14px;flex-wrap:wrap">
		${card(d.bright_background || "#ffffff", d.bright_surface || "#f6f7f8", "#0a0a0a", "#5f6368", brightInk, "#0a0a0a", "#fff", accent + "33", logo(brightLogo, !d.bright_logo))}
		${card(d.dark_background || "#000000", d.dark_surface || "#0b0b0b", "#fff", "#a3a3a3", accent, "#fff", "#000", accent + "55", logo(darkLogo, false))}
	</div><p class="text-muted small" style="margin-top:8px">${__("Left: Bright mode (default). Right: Dark mode. Each user switches with the toggle on the login page.")}</p>`);
}
