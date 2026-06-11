frappe.ui.form.on("Tally Migration Log", {
	refresh(frm) {
		render_summary(frm);
		render_reconciliation(frm);
		render_created(frm);
		render_quality(frm);
		render_edits(frm);
		render_coverage(frm);
		render_mapping(frm);
		add_buttons(frm);
	},
});

// ── Field-coverage report ───────────────────────────────────────────────────
// Lists fields present in the uploaded Tally file that the migrator does NOT
// read (UDFs / unmapped attributes) - i.e. data that never entered the pipeline.
// Read-only audit of what was intentionally left behind.

function render_coverage(frm) {
	const field = frm.get_field("coverage_view");
	if (!field) return;
	const wrapper = field.$wrapper;
	wrapper.empty();

	let report = null;
	try {
		report = JSON.parse(frm.doc.coverage_report || "null");
	} catch (e) {
		report = null;
	}
	if (!report || !report.types) return;

	const esc = frappe.utils.escape_html;
	if (report.clean || !report.types.length) {
		wrapper.html(
			`<div class="text-success" style="padding:6px 0;">
				✓ Every field in your file maps to an ERPNext field - nothing was left behind.
			</div>`
		);
		return;
	}

	const fieldTable = (list, valueLabel) => {
		const rows = list
			.map(
				(u) => `
				<tr>
					<td style="font-family:monospace;">${esc(u.field)}</td>
					<td class="text-right text-muted">${u.count}</td>
					<td class="text-muted">${u.sample ? esc(String(u.sample)) : ""}</td>
					<td class="text-muted small">${(u.examples || []).map(esc).join(", ")}</td>
				</tr>`
			)
			.join("");
		return `
			<table class="table table-condensed" style="margin:0 0 6px;">
				<thead><tr>
					<th style="border-top:0;">Field</th>
					<th style="border-top:0;" class="text-right">Count</th>
					<th style="border-top:0;">${esc(valueLabel)}</th>
					<th style="border-top:0;">Example records</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>`;
	};

	const blocks = report.types
		.map((t) => {
			const unmapped = (t.unmapped || []).length
				? `<div class="text-muted small" style="margin:2px 0;">Not read from the file (custom fields / UDFs):</div>${fieldTable(t.unmapped, "Sample value")}`
				: "";
			const unwritten = (t.unwritten || []).length
				? `<div class="small" style="margin:6px 0 2px; color:#f0a500;">⚠ Read but not written to ERPNext:</div>${fieldTable(t.unwritten, "Sample value")}`
				: "";
			return `
				<div style="margin-bottom:12px;">
					<div style="font-weight:600; margin-bottom:4px;">${esc(t.entity_type)}</div>
					${unmapped}${unwritten}
				</div>`;
		})
		.join("");

	const unwrittenCount = report.unwritten_field_count || 0;
	const unwrittenNote = unwrittenCount
		? `<div class="small" style="margin-bottom:8px; color:#f0a500;">
				<strong>${unwrittenCount}</strong> field(s) were read from the file but
				<strong>not persisted</strong> to ERPNext - review these, they are a real gap.
			</div>`
		: "";

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:12px 16px; margin:8px 0;">
			<div class="text-muted small" style="margin-bottom:8px;">
				<strong>${report.unmapped_field_count}</strong> field(s) in your file were
				<strong>not migrated</strong> (Tally custom fields / attributes outside the
				supported mapping). The records themselves still imported.
			</div>
			${unwrittenNote}
			${blocks}
		</div>
	`);
}

// ── Accounts-mapping audit ──────────────────────────────────────────────────
// Durable record of how each Tally ledger was classified into ERPNext accounts,
// the rows whose nature had to be inferred (no reserved Tally ancestor), and the
// opening-balance residual that posts to Temporary Opening. Mirrors the Review
// step the user saw pre-flight, kept here so the classification is reviewable
// after the run. Derived from the resolver - no hand-maintained labels.

function render_mapping(frm) {
	const field = frm.get_field("mapping_view");
	if (!field) return;
	const wrapper = field.$wrapper;
	wrapper.empty();

	let m = null;
	try {
		m = JSON.parse(frm.doc.mapping_report || "null");
	} catch (e) {
		m = null;
	}
	if (!m || !m.total_accounts) return;

	const esc = frappe.utils.escape_html;
	const fmt = (n) => Number(n || 0).toLocaleString("en-IN");
	const inferred = m.inferred_count || 0;
	const confident = m.total_accounts - inferred;
	const plug = m.opening || {};
	const ob = (r) =>
		r.amount ? `${fmt(r.amount)} <span class="text-muted">${esc(r.dr_cr)}</span>` : `<span class="text-muted">0</span>`;
	const classifiedAs = (r) => esc(r.root_type) + (r.account_type ? ` · ${esc(r.account_type)}` : "");

	const plugLine = plug.clean
		? `<span class="text-success">✓ opening balances balanced (Dr = Cr)</span>`
		: `<span style="color:#f0a500;">⚠ <strong>${fmt(plug.temporary_opening_plug)} ${esc(
				plug.plug_dr_cr
		  )}</strong> posted to Temporary Opening</span>`;

	const inferredBlock = inferred
		? `<div class="small" style="margin:8px 0 4px; color:#f0a500;">
				⚠ <strong>${fmt(inferred)}</strong> account(s) had no standard Tally group - their type was inferred:
			</div>
			<table class="table table-condensed" style="margin:0;">
				<thead><tr>
					<th style="border-top:0;">Tally ledger</th>
					<th style="border-top:0;">Classified as</th>
					<th style="border-top:0;" class="text-right">Opening</th>
				</tr></thead>
				<tbody>${m.inferred
					.map(
						(r) => `<tr>
							<td>${esc(r.name)}</td>
							<td class="text-muted">${classifiedAs(r)}</td>
							<td class="text-right">${ob(r)}</td>
						</tr>`
					)
					.join("")}</tbody>
			</table>`
		: `<div class="text-success small" style="margin:8px 0;">
				✓ All ${fmt(m.total_accounts)} accounts mapped using Tally's standard groups - none inferred.
			</div>`;

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:12px 16px; margin:8px 0;">
			<div class="text-muted small" style="margin-bottom:4px;">
				<strong>${fmt(m.total_accounts)}</strong> ledger account(s) classified -
				<strong>${fmt(confident)}</strong> by Tally's standard groups, <strong>${fmt(inferred)}</strong> inferred.
			</div>
			<div class="small" style="margin-bottom:4px;">${plugLine}</div>
			${inferredBlock}
		</div>
	`);
}

// ── Reconciliation: opening Trial Balance, Tally vs ERPNext ──────────────────
// Read-only. The opening trial balance built from Tally's figures beside what
// ERPNext now holds (read back from the GL / stock), per account class plus the
// Debtors/Creditors/stock control lines, with a balanced Dr = Cr total. The
// Temporary Opening row is Tally's own "Difference in Opening Balances", so a
// non-zero value there is a faithful migration, not a gap. Informational - no gate.

function render_reconciliation(frm) {
	const field = frm.get_field("reconciliation_view");
	if (!field) return;
	const wrapper = field.$wrapper;
	wrapper.empty();

	let r = null;
	try {
		r = JSON.parse(frm.doc.reconciliation_report || "null");
	} catch (e) {
		r = null;
	}
	if (!r || !r.rows || !r.rows.length) return;

	const esc = frappe.utils.escape_html;
	const fmt = (n) =>
		Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	// One amount cell: shows the figure only in the column (Dr/Cr) it belongs to.
	const dr = (s) => (s && s.dr_cr === "Dr" && s.amount ? fmt(s.amount) : "");
	const cr = (s) => (s && s.dr_cr === "Cr" && s.amount ? fmt(s.amount) : "");
	const avail = r.available;

	const VERDICT = {
		reconciled: { color: "#28a745", text: "✓ Reconciled - the opening trial balance matches ERPNext." },
		review: {
			color: "#e24c4c",
			text: "⚠ A figure differs between Tally and ERPNext - review the rows below.",
		},
		source_only: {
			color: "#6c757d",
			text: "ERPNext figures could not be read back; showing Tally's trial balance only.",
		},
	};
	const v = VERDICT[r.verdict] || VERDICT.source_only;

	const rows = r.rows
		.map((row) => {
			const stat = !row.has_erpnext
				? ""
				: row.match
				? `<span class="text-success">✓</span>`
				: `<span class="text-danger">⚠</span>`;
			const note = row.is_opening_difference
				? `<div class="text-muted small" style="font-weight:400;">Tally's own "Difference in Opening Balances" - a non-zero value here is faithful, not a gap.</div>`
				: "";
			const erpDr = avail && row.has_erpnext ? dr(row.erpnext) : "";
			const erpCr = avail && row.has_erpnext ? cr(row.erpnext) : "";
			return `
				<tr>
					<td style="font-weight:600; white-space:nowrap;">${esc(row.label)}${note}</td>
					<td class="text-right" style="vertical-align:top;">${dr(row.source)}</td>
					<td class="text-right" style="vertical-align:top;">${cr(row.source)}</td>
					<td class="text-right text-muted" style="vertical-align:top;">${erpDr}</td>
					<td class="text-right text-muted" style="vertical-align:top;">${erpCr}</td>
					<td class="text-center" style="vertical-align:top;">${stat}</td>
				</tr>`;
		})
		.join("");

	const t = r.total || { source: {}, erpnext: {} };
	const bal = (ok) =>
		ok ? `<span class="text-success" title="Dr = Cr">✓</span>` : `<span class="text-danger">⚠</span>`;
	const foot = `
		<tr style="border-top:2px solid #e0e6ed; font-weight:600;">
			<td>Total</td>
			<td class="text-right">${fmt(t.source.dr)}</td>
			<td class="text-right">${fmt(t.source.cr)}</td>
			<td class="text-right text-muted">${avail ? fmt(t.erpnext.dr) : ""}</td>
			<td class="text-right text-muted">${avail ? fmt(t.erpnext.cr) : ""}</td>
			<td class="text-center">${bal(t.source_balanced)}</td>
		</tr>`;

	const stockNote = r.stock_items
		? `<div class="text-muted small" style="margin-top:8px;">Stock value across ${r.stock_items} item(s) with opening quantity. Receivables/Payables are the Debtors/Creditors control totals (shown separately from Assets/Liabilities).</div>`
		: "";

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:12px 16px; margin:8px 0;">
			<div class="small" style="margin-bottom:8px; color:${v.color};">${v.text}</div>
			<table class="table table-condensed" style="margin:0;">
				<thead>
					<tr>
						<th style="border-top:0;" rowspan="2" class="text-muted small">Account class</th>
						<th style="border-top:0;" colspan="2" class="text-center">Tally</th>
						<th style="border-top:0;" colspan="2" class="text-center text-muted">ERPNext</th>
						<th style="border-top:0;" rowspan="2"></th>
					</tr>
					<tr>
						<th class="text-right small" style="border-top:0;">Dr</th>
						<th class="text-right small" style="border-top:0;">Cr</th>
						<th class="text-right small text-muted" style="border-top:0;">Dr</th>
						<th class="text-right small text-muted" style="border-top:0;">Cr</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
				<tfoot>${foot}</tfoot>
			</table>
			${stockNote}
		</div>
	`);
}

// ── Records-created audit trail ─────────────────────────────────────────────
// The authoritative "what did this run touch" list: every ERPNext document this
// migration inserted, grouped by entity, with deep links - including the opening
// Journal Entry and Stock Reconciliation, so the run is reviewable / reversible.

const CREATED_DOCTYPE = {
	"Accounts": "Account",
	"Cost Centres": "Cost Center",
	"Warehouses": "Warehouse",
	"Units": "UOM",
	"Stock Groups": "Item Group",
	"Customers": "Customer",
	"Suppliers": "Supplier",
	"Items": "Item",
	"Opening Balances": "Journal Entry",
	"Opening Stock": "Stock Reconciliation",
};

function render_created(frm) {
	const field = frm.get_field("created_view");
	if (!field) return;
	const wrapper = field.$wrapper;
	wrapper.empty();

	let created = {};
	try {
		created = JSON.parse(frm.doc.created_records || "{}");
	} catch (e) {
		created = {};
	}
	const entries = Object.entries(created).filter(([, names]) => (names || []).length);
	if (!entries.length) return;

	const esc = frappe.utils.escape_html;
	const total = entries.reduce((n, [, names]) => n + names.length, 0);

	const blocks = entries
		.map(([label, names]) => {
			const dt = CREATED_DOCTYPE[label];
			const links = names
				.map((nm) => {
					const safe = esc(nm);
					return dt
						? `<a href="/app/${encodeURIComponent(
								frappe.router.slug(dt)
						  )}/${encodeURIComponent(nm)}" target="_blank">${safe}</a>`
						: safe;
				})
				.join(", ");
			return `
				<div style="margin-bottom:8px;">
					<div style="font-weight:600; margin-bottom:2px;">
						${esc(label)} <span class="text-muted" style="font-weight:400;">(${names.length})</span>
					</div>
					<div class="small" style="line-height:1.8;">${links}</div>
				</div>`;
		})
		.join("");

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:12px 16px; margin:8px 0;">
			<div class="text-muted small" style="margin-bottom:8px;">
				<strong>${total}</strong> ERPNext document(s) were created by this run.
				Use these to review or reverse the migration.
			</div>
			${blocks}
		</div>
	`);
}

// ── Applied-edits audit trail ───────────────────────────────────────────────
// Renders the exact pre-flight (step 3) edits that were applied to the data
// before import: which field on which record changed, old → new. The source
// XML is never modified, so this is the authoritative record of what changed.

function render_edits(frm) {
	const field = frm.get_field("edits_view");
	if (!field) return;
	const wrapper = field.$wrapper;
	wrapper.empty();

	let edits = [];
	try {
		edits = JSON.parse(frm.doc.applied_edits || "[]");
	} catch (e) {
		edits = [];
	}
	if (!edits.length) return;

	const esc = frappe.utils.escape_html;
	const blank = '<span class="text-muted">(blank)</span>';
	const rows = edits
		.map(
			(e) => `
			<tr>
				<td><span class="text-muted">${esc(e.entity_type || "")}</span> · ${esc(e.record_name || "")}</td>
				<td>${esc(e.field || "")}</td>
				<td class="text-muted">${e.old ? esc(String(e.old)) : blank}</td>
				<td>→</td>
				<td class="text-success">${e.new ? esc(String(e.new)) : blank}</td>
			</tr>`
		)
		.join("");

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:12px 16px; margin:8px 0;">
			<div class="text-muted small" style="margin-bottom:6px;">
				<strong>${edits.length}</strong> field edit(s) were applied on the pre-flight
				screen before this run. The uploaded file was not modified.
			</div>
			<table class="table table-condensed" style="margin:0;">
				<thead>
					<tr>
						<th style="border-top:0;">Record</th>
						<th style="border-top:0;">Field</th>
						<th style="border-top:0;">From</th>
						<th style="border-top:0;"></th>
						<th style="border-top:0;">To</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
			</table>
		</div>
	`);
}

// ── Pre-flight data-quality report ──────────────────────────────────────────
// Renders the stored grouped validation report (errors/warnings by rule code)
// captured before the migration ran.

function render_quality(frm) {
	const field = frm.get_field("quality_view");
	if (!field) return;
	const wrapper = field.$wrapper;
	wrapper.empty();

	let report = null;
	try {
		report = JSON.parse(frm.doc.validation_report || "null");
	} catch (e) {
		report = null;
	}
	if (!report || !report.groups) return;

	const esc = frappe.utils.escape_html;

	if (report.clean || !report.groups.length) {
		wrapper.html(
			`<div class="text-success" style="padding:6px 0;">
				✓ No data-quality issues were flagged before this run.
			</div>`
		);
		return;
	}

	const LABELS = {
		GSTIN_INVALID: "Invalid GSTIN",
		GST_STATE_MISSING: "GST state missing",
		GSTIN_STATE_MISMATCH: "GSTIN / state mismatch",
		PIN_STATE_CONFLICT: "PIN / state conflict",
		HSN_MISSING: "HSN code missing",
		ITEM_CODE_COLLISION: "Item code collision",
		DUPLICATE_PARTY: "Possible duplicate party",
	};

	const rows = report.groups
		.map((g) => {
			const isErr = g.severity === "error";
			const dot = isErr ? "#e24c4c" : "#f0a500";
			const label = LABELS[g.code] || g.code;
			const items = g.items
				.map(
					(it) =>
						`<div style="padding:2px 0; color:#555;">
							<span class="text-muted">${esc(it.entity_type)}</span> · ${esc(it.entity_name)}
						</div>`
				)
				.join("");
			return `
				<div style="border-top:1px solid #f0f4f7; padding:8px 0;">
					<div style="font-weight:600;">
						<span style="color:${dot};">■</span> ${esc(label)}
						<span class="text-muted" style="font-weight:400;">(${g.items.length})</span>
					</div>
					${g.fix_hint ? `<div class="text-muted small" style="margin:2px 0 4px;">${esc(g.fix_hint)}</div>` : ""}
					<div style="margin-left:14px;">${items}</div>
				</div>`;
		})
		.join("");

	wrapper.html(`
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:12px 16px; margin:8px 0;">
			<div style="margin-bottom:6px;">
				<span class="text-danger"><strong>${report.error_count}</strong> error(s)</span>
				&nbsp;·&nbsp;
				<span style="color:#f0a500;"><strong>${report.warning_count}</strong> warning(s)</span>
				<span class="text-muted small">- flagged before this run</span>
			</div>
			${rows}
		</div>
	`);
}

// ── Visual summary dashboard ────────────────────────────────────────────────
// Turns the stored import_summary JSON into a scannable per-entity breakdown
// (created / already there / failed) with a stacked bar - no JSON reading.

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
		<div style="border:1px solid #e0e6ed; border-radius:8px; padding:14px 16px; margin:8px 0;">
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
