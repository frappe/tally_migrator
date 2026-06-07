frappe.ui.form.on("Tally Migration Log", {
	refresh(frm) {
		render_summary(frm);
		add_buttons(frm);
	},
});

// ── Visual summary dashboard ────────────────────────────────────────────────
// Turns the stored import_summary JSON into a scannable per-entity breakdown
// (created / already there / failed) with a stacked bar — no JSON reading.

function render_summary(frm) {
	const wrapper = frm.get_field("summary_view").$wrapper;
	wrapper.empty();

	let summary = {};
	try {
		summary = JSON.parse(frm.doc.import_summary || "{}");
	} catch (e) {
		summary = {};
	}

	const entries = Object.entries(summary);
	if (!entries.length) {
		wrapper.html(
			`<div class="text-muted" style="padding:6px 0;">
				${frm.doc.status === "Running"
					? "Migration is still running…"
					: "No import summary recorded for this run."}
			</div>`
		);
		return;
	}

	const esc = frappe.utils.escape_html;
	let totalCreated = 0,
		totalSkipped = 0,
		totalFailed = 0;

	const rows = entries
		.map(([label, r]) => {
			const created = r.created || 0;
			const skipped = r.skipped || 0;
			const failed = r.failed || 0;
			totalCreated += created;
			totalSkipped += skipped;
			totalFailed += failed;
			const total = created + skipped + failed || 1;
			const pct = (n) => (n / total) * 100;

			const bar = `
				<div style="display:flex; height:8px; border-radius:4px; overflow:hidden; background:#f0f4f7;">
					<div style="width:${pct(created)}%; background:#28a745;"></div>
					<div style="width:${pct(skipped)}%; background:#d1d8dd;"></div>
					<div style="width:${pct(failed)}%; background:#e24c4c;"></div>
				</div>`;

			return `
				<tr>
					<td style="font-weight:600; white-space:nowrap; vertical-align:middle;">${esc(label)}</td>
					<td style="width:45%; vertical-align:middle;">${bar}</td>
					<td class="text-right text-success" style="vertical-align:middle;">${created}</td>
					<td class="text-right text-muted" style="vertical-align:middle;">${skipped}</td>
					<td class="text-right ${failed ? "text-danger" : "text-muted"}" style="vertical-align:middle;">
						${failed ? `<strong>${failed}</strong>` : failed}
					</td>
				</tr>`;
		})
		.join("");

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:14px 16px;">
			<table class="table table-condensed" style="margin:0;">
				<thead>
					<tr>
						<th style="border-top:0;">Record type</th>
						<th style="border-top:0;"></th>
						<th style="border-top:0;" class="text-right">Imported</th>
						<th style="border-top:0;" class="text-right">Already there</th>
						<th style="border-top:0;" class="text-right">Failed</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
				<tfoot>
					<tr style="border-top:2px solid #e0e6ed;">
						<td style="font-weight:600;">Total</td>
						<td></td>
						<td class="text-right text-success"><strong>${totalCreated}</strong></td>
						<td class="text-right text-muted">${totalSkipped}</td>
						<td class="text-right ${totalFailed ? "text-danger" : "text-muted"}">
							${totalFailed ? `<strong>${totalFailed}</strong>` : totalFailed}
						</td>
					</tr>
				</tfoot>
			</table>
			<div class="text-muted small" style="margin-top:10px; line-height:1.6;">
				<span style="color:#28a745;">■</span> Imported (new) &nbsp;·&nbsp;
				<span style="color:#aeb8c2;">■</span> Already there (skipped, safe) &nbsp;·&nbsp;
				<span style="color:#e24c4c;">■</span> Failed
			</div>
		</div>
	`);
}

// ── Action buttons ──────────────────────────────────────────────────────────

function add_buttons(frm) {
	if (frm.is_new()) return;

	// Re-run is only meaningful when there's a source file AND something to retry.
	const canRetry =
		frm.doc.source_file &&
		(frm.doc.status === "Completed with Errors" || frm.doc.status === "Failed");

	if (canRetry) {
		frm.add_custom_button(__("Re-run from Source File"), () => {
			frappe.confirm(
				__(
					"This re-runs the migration from the same file. Records that already " +
						"imported are skipped, so in practice only the previously failed ones are retried. " +
						"A new log will be created. Continue?"
				),
				() => rerun(frm)
			);
		}).addClass("btn-primary");
	}

	frm.add_custom_button(__("Open Tally Migrator"), () => {
		frappe.set_route("tally-migrator");
	});
}

function rerun(frm) {
	frappe.dom.freeze(__("Re-running migration…"));
	frappe.call({
		method: "tally_migrator.api.rerun_from_log",
		args: { log_name: frm.doc.name },
		callback: (r) => {
			frappe.dom.unfreeze();
			const res = r.message || {};
			const newLog = res.log_name;
			frappe.show_alert({ message: __("Re-run complete."), indicator: "green" });
			if (newLog && newLog !== frm.doc.name) {
				frappe.set_route("Form", "Tally Migration Log", newLog);
			} else {
				frm.reload_doc();
			}
		},
		error: () => frappe.dom.unfreeze(),
	});
}
