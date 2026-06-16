frappe.ui.form.on("Tally Migration Log", {
	refresh(frm) {
		apply_dark_mode_theme(frm);
		render_summary(frm);
		render_reconciliation(frm);
		render_created(frm);
		render_quality(frm);
		render_edits(frm);
		render_coverage(frm);
		render_mapping(frm);
		add_buttons(frm);
		/* ===== ROLLBACK FEATURE - remove this line + the fenced block below to delete ===== */
		tally_rollback_attach(frm);
	},
});

// ── Dark-mode theming ────────────────────────────────────────────────────────
// Frappe flips its semantic tokens in dark mode (--text-color, --border-color…)
// but never its palette tints (--blue-100, --green-100, --red-100, --gray-100…),
// which our callouts/cards paint as backgrounds under --text-color text. Re-point
// just those tints to Frappe's dark surfaces, scoped to this form, so every
// existing var(--blue-100, …) resolves correctly in both themes. Mirrors the
// wizard's TallyMigratorPage.themeStyle(); injected once per form load.
function apply_dark_mode_theme(frm) {
	frm.$wrapper.addClass("tally-migration-log");
	if (frm.$wrapper.find("style.tally-dark-theme").length) return;
	frm.$wrapper.prepend(`<style class="tally-dark-theme">
		[data-theme="dark"] .tally-migration-log {
			--blue-100: #0e2037;   --blue-200: #052b53;
			--green-100: #0b2e1c;  --green-200: #0a3f27;
			--red-100: #361515;    --red-200: #521515;
			--gray-100: #232323;   --gray-200: #2b2b2b;   --gray-300: #343434;
		}
		/* Replace the browser's default <details> triangle with Frappe's SVG caret
		   (a "right" icon that rotates to point down when open), so disclosure
		   markers match the carets used elsewhere instead of a black glyph. */
		.tally-migration-log details > summary { list-style: none; }
		.tally-migration-log details > summary::-webkit-details-marker { display: none; }
		.tally-migration-log details > summary .tm-caret {
			display: inline-flex; align-items: center; vertical-align: middle;
			color: ${MUTED}; transition: transform 0.15s ease;
		}
		.tally-migration-log details[open] > summary .tm-caret { transform: rotate(90deg); }
		/* Inline info tooltip, identical to the wizard's infoTip: a small muted
		   outline (i) that reveals a styled bubble on hover/focus. Uses currentColor
		   so it stays quiet, never a loud filled blue icon. */
		.tally-migration-log .tm-tip {
			position: relative; display: inline-flex; align-items: center;
			vertical-align: -0.12em; margin-left: 6px;
			color: var(--text-muted, #999); cursor: help;
		}
		.tally-migration-log .tm-tip-icon { display: block; }
		.tally-migration-log .tm-tip:hover { color: var(--text-color, #1f272e); }
		.tally-migration-log .tm-tip-bubble {
			visibility: hidden; opacity: 0;
			position: absolute; bottom: 145%; left: 50%; transform: translateX(-50%);
			width: 240px; max-width: 70vw;
			background: var(--text-color, #1f272e); color: var(--bg-color, #fff);
			text-align: left; font-size: 12px; line-height: 1.45; font-weight: 400;
			padding: 8px 10px; border-radius: 6px;
			box-shadow: 0 4px 14px rgba(0,0,0,0.18); z-index: 1000;
			transition: opacity 0.12s ease; pointer-events: none; white-space: normal;
		}
		.tally-migration-log .tm-tip:hover .tm-tip-bubble,
		.tally-migration-log .tm-tip:focus .tm-tip-bubble { visibility: visible; opacity: 1; }
	</style>`);
}

// ── Shared UI vocabulary ─────────────────────────────────────────────────────
// One design language across the whole log, identical to the Tally Migrator
// wizard: tinted callouts (no raw orange/red sentences), filled-circle status
// icons centred on the first text line, CSS-token colours (never bare hex that
// drifts), 500-weight bold, and long record lists collapsed behind a count.

const TEXT = "var(--text-color, #1f272e)";
const MUTED = "var(--text-muted, #6c757d)";
const BORDER = "var(--gray-300, #d1d8dd)";
// Bar/legend fills - declared once so the stacked bar and its legend can never
// drift apart (they did: bar grey was #d1d8dd, legend swatch was #aeb8c2).
const BAR_CREATED = "var(--green-500, #30a66d)";
const BAR_SKIPPED = "var(--gray-400, #c0c8d0)";
const BAR_FAILED = "var(--red-500, #e24c4c)";

// Sections sit directly on the form surface (each already has a native Frappe
// section header), rather than each being re-boxed in an identical border. This
// keeps the information on the surface and avoids seven look-alike cards stacking
// into grid-like sameness. Semantic colour still rides on the inner callouts.
const CARD = "";

// Single source of truth for vertical rhythm. EVERY render path - boxed card or
// a bare clean-state callout - is wrapped in this, so the gap above and below is
// always identical and nothing ever touches the next section heading.
function section(html) {
	return `<div style="margin:8px 0 18px;">${html}</div>`;
}

function statusIcon(kind) {
	const name = { success: "solid-success", info: "solid-info", error: "solid-error" }[kind] || "solid-info";
	// line-height:0 kills the baseline gap; the small negative vertical-align
	// drops the 16px glyph onto the text baseline so inline icons sit level
	// (in a flex iconRow vertical-align is ignored, so block usage is unaffected).
	return `<span style="display:inline-flex; align-items:center; line-height:0; vertical-align:-0.18em;">${frappe.utils.icon(name, "sm")}</span>`;
}

// An inline SVG arrow (from Frappe's icon sprite) for "from → to" / "maps to"
// relationships, so the UI never falls back to a text glyph as an icon.
function arrowIcon() {
	return `<span style="display:inline-flex; align-items:center; vertical-align:middle; color:${MUTED};">${frappe.utils.icon("right", "sm")}</span>`;
}

// Disclosure caret for a native <details> summary - the CSS in
// apply_dark_mode_theme() hides the browser triangle and rotates this to point
// down when the section is open, matching the wizard's caret vocabulary.
function caretMarker() {
	return `<span class="tm-caret">${frappe.utils.icon("right", "sm")}</span>`;
}

// Inline info tooltip: a small muted outline (i) that reveals `text` on hover or
// focus. Identical markup to the wizard's TallyMigratorPage.infoTip so the two
// surfaces look the same; styled by the .tm-tip rules in apply_dark_mode_theme().
function infoTip(text) {
	const safe = frappe.utils.escape_html(text);
	const icon =
		`<svg class="tm-tip-icon" width="10" height="10" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
		`<circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.1"/>` +
		`<line x1="8" y1="7.2" x2="8" y2="11.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>` +
		`<circle cx="8" cy="4.8" r="0.8" fill="currentColor"/></svg>`;
	return `<span class="tm-tip" tabindex="0" role="note" aria-label="${safe}">${icon}<span class="tm-tip-bubble">${safe}</span></span>`;
}

// One canonical explanation of the Temporary Opening residual, reused by every
// surface (recon tooltip, mapping callout) so the wording never drifts. Mirrors
// the wizard plug tip and the saved warning in importers.py.
const TEMP_OPENING_NOTE =
	"It is the part of your Tally opening that does not balance on its own, plus any " +
	"income/expense opening balances ERPNext cannot carry. This is normal - clear it " +
	"as you finish your opening entries.";

// Icon + content, with the 16px icon optically centred on the first line of text
// (a 1.5em flex box), so headings of any size sit level with their marker.
function iconRow(kind, html) {
	return `<div style="display:flex; align-items:flex-start; gap:8px;">
		<span style="flex:0 0 auto; display:inline-flex; align-items:center; height:1.5em;">${statusIcon(kind)}</span>
		<div style="flex:1; min-width:0;">${html}</div>
	</div>`;
}

// Tinted notice box: blue=info (non-blocking), green=success, red=error.
function callout(kind, inner, extraStyle = "") {
	const t = {
		info: ["var(--blue-100, #edf6fd)", "var(--blue-200, #e3f1fd)"],
		success: ["var(--green-100, #e4f5e9)", "var(--green-200, #daf0e1)"],
		error: ["var(--red-100, #fff0f0)", "var(--red-200, #fcd7d7)"],
	}[kind] || ["var(--blue-100, #edf6fd)", "var(--blue-200, #e3f1fd)"];
	return `<div style="background:${t[0]}; border:1px solid ${t[1]}; border-radius:8px; padding:11px 13px; color:${TEXT};${extraStyle}">${inner}</div>`;
}

// Long record lists fold behind a count once they pass the threshold, mirroring
// the collapsible COA book in the wizard. Short lists render inline.
const COLLAPSE_AT = 8;
function collapsible(count, summaryText, innerHtml) {
	if (count <= COLLAPSE_AT) return innerHtml;
	return `<details style="margin-top:4px;">
		<summary style="cursor:pointer; user-select:none; color:${MUTED}; font-size:12px;">${caretMarker()}<span style="margin-left:6px;">${summaryText}</span></summary>
		<div style="margin-top:8px;">${innerHtml}</div>
	</details>`;
}

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

	// Step 2: a plain-language phrase for a field's derived shape, so the audit reads
	// "looks like a Yes/No flag" instead of a bare tag name. Falls back to the value-
	// kind (or nothing) for older logs that predate the classifier.
	const SHAPE_LABEL = {
		boolean: "Yes/No flag",
		select: "category",
		identifier: "reference / ID",
		constant: "constant (same on every record)",
		freetext: "free text",
	};
	const shapeText = (u) => {
		if (u.shape == null) return u.kind || ""; // legacy log
		if (u.shape === "typed") return u.kind || "typed value";
		let s = SHAPE_LABEL[u.shape] || u.shape;
		if (u.shape === "select" && (u.options || []).length)
			s += `: ${u.options.slice(0, 6).map(esc).join(", ")}`;
		return s;
	};
	// Richer unmapped table: adds "What it looks like" (shape) and fill-rate columns
	// so the user can judge each field at a glance, not just see its name.
	const unmappedTable = (list) => {
		const rows = list
			.map(
				(u) => `
				<tr>
					<td style="font-family:monospace; word-break:break-word;">${esc(u.field)}</td>
					<td class="text-muted small" style="word-break:break-word;">${esc(shapeText(u))}</td>
					<td class="text-right text-muted">${u.fill_rate != null ? Math.round(u.fill_rate * 100) + "%" : ""}</td>
					<td class="text-muted" style="word-break:break-word;">${u.sample ? esc(String(u.sample)) : ""}</td>
				</tr>`
			)
			.join("");
		// Fixed layout + explicit column widths so headers never wrap and a long
		// "category: a, b, c…" value can't blow one column out - it wraps in place.
		return `
			<table class="table table-condensed" style="margin:0 0 6px; table-layout:fixed; width:100%;">
				<colgroup>
					<col style="width:26%;">
					<col style="width:34%;">
					<col style="width:12%;">
					<col style="width:28%;">
				</colgroup>
				<thead><tr>
					<th style="border-top:0; white-space:nowrap;">Field</th>
					<th style="border-top:0; white-space:nowrap;">What it looks like</th>
					<th style="border-top:0; white-space:nowrap;" class="text-right">Filled</th>
					<th style="border-top:0; white-space:nowrap;">Sample value</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>`;
	};

	// Loss tables (UDFs we never read + read-but-not-persisted), one block per type
	// that actually has a loss - this is what makes a file "un-clean".
	const lossTypes = report.types.filter(
		(t) => (t.unmapped || []).length || (t.unwritten || []).length
	);
	// Whether the classifier ran (new logs). When it did, split each type's unmapped
	// fields into the ones that look like real business data and the zero-information
	// constants, so the latter can be tucked into a collapser instead of alarming.
	const haveScores = report.meaningful_unmapped_count != null;
	const isMeaningful = (u) => u.score == null || u.score >= 0.2;
	const blocks = lossTypes
		.map((t) => {
			const all = t.unmapped || [];
			const meaningful = haveScores ? all.filter(isMeaningful) : all;
			const trivial = haveScores ? all.filter((u) => !isMeaningful(u)) : [];
			const unmapped = all.length
				? (meaningful.length
						? `<div class="text-muted small" style="margin:2px 0;">Not read from the file (custom fields / UDFs):</div>${unmappedTable(meaningful)}`
						: "") +
				  (trivial.length
						? collapsible(
								trivial.length + 1, // always collapsed: reassurance-only detail
								`Show ${trivial.length} config constant(s) - same value on every record, no business data`,
								unmappedTable(trivial)
						  )
						: "")
				: "";
			const unwritten = (t.unwritten || []).length
				? `<div class="small" style="margin:6px 0 2px; color:${TEXT}; display:flex; align-items:center; gap:6px;">${statusIcon("info")}<span>Read but not written to ERPNext:</span></div>${fieldTable(t.unwritten, "Sample value")}`
				: "";
			return `
				<div style="margin-bottom:12px;">
					<div style="font-weight:500; margin-bottom:4px;">${esc(t.entity_type)}</div>
					${unmapped}${unwritten}
				</div>`;
		})
		.join("");

	// Full no-loss audit: every Tally-internal field we suppressed is still listed
	// here on demand, so a skeptical user can confirm nothing of value was hidden.
	// (Older logs predate the itemised noise list, so guard for its absence.)
	const noiseBlocks = report.types
		.filter((t) => (t.noise || []).length)
		.map(
			(t) => `
			<div style="margin-bottom:12px;">
				<div style="font-weight:500; margin-bottom:4px;">${esc(t.entity_type)}</div>
				${fieldTable(t.noise, "Sample value")}
			</div>`
		)
		.join("");
	const noiseCount = report.noise_field_count || 0;
	const noiseAudit = noiseBlocks
		? collapsible(
				COLLAPSE_AT + 1, // always collapse this verbose, reassurance-only detail
				`Show all ${noiseCount} Tally-internal field(s) we set aside`,
				`<div class="text-muted small" style="margin:2px 0 8px;">Config flags, empty
					containers, audit stamps and pre-GST tax scaffolding - no business value,
					listed here so nothing is hidden.</div>${noiseBlocks}`
		  )
		: "";

	// Reconciliation line: account for every tag in the file by bucket, so the audit
	// reads as a closed balance ("all N accounted for"), not just a loss count. Shown
	// only when the new totals are present (older logs omit them).
	let recon = "";
	if (report.total_tag_count != null) {
		const internal = (report.noise_field_count || 0) + (report.ignored_field_count || 0);
		const loss = (report.unmapped_field_count || 0) + (report.unwritten_field_count || 0);
		const parts = [
			`<strong>${report.imported_field_count || 0}</strong> imported`,
			(report.redundant_field_count || 0) ? `<strong>${report.redundant_field_count}</strong> already captured via another field` : "",
			internal ? `<strong>${internal}</strong> Tally-internal` : "",
			loss ? `<strong>${loss}</strong> not migrated` : "",
		].filter(Boolean).join(" · ");
		const mismatch = report.accounted_for === false;
		recon = callout(
			mismatch ? "error" : "info",
			iconRow(
				mismatch ? "error" : "info",
				mismatch
					? `Coverage accounting did not reconcile (${report.total_tag_count} fields in file). Please report this log.`
					: `All <strong>${report.total_tag_count}</strong> fields in your file are accounted for: ${parts}. Nothing was dropped without a record.`
			),
			"margin-bottom:8px;"
		);
	}

	// Current tax frameworks (TDS/TCS) present but deliberately not migrated - named
	// so the scope decision reads as intentional, not a silent drop.
	const recognized = report.recognized_not_migrated || [];
	const recognizedNote = recognized.length
		? callout(
				"info",
				iconRow(
					"info",
					`We detected <strong>${recognized.map((t) => esc(t)).join(" and ")}</strong>
					in this file. This migration brings over master records and opening balances
					only - it does not migrate tax-deduction configuration. Your ledger balances
					are preserved in full.`
				),
				"margin-bottom:8px;"
		  )
		: "";

	// Employees that Tally exports as Cost Centres carry payroll/HR fields (gender,
	// PF, bank, dates). Named as a deliberate skip - not guessed value-shapes or a
	// silent loss - mirroring the tax note above; the cost centres themselves import.
	const hrNote = report.hr_not_migrated
		? callout(
				"info",
				iconRow(
					"info",
					`We detected <strong>${esc(report.hr_not_migrated)}</strong> on cost
					centres Tally uses for employees. Payroll / HR migration is out of scope,
					so these fields are not migrated; the cost centres themselves import in
					full. Bring employees across separately in HR / Payroll.`
				),
				"margin-bottom:8px;"
		  )
		: "";

	if (!lossTypes.length) {
		// No real loss: lead with reassurance, but still offer the full audit beneath.
		wrapper.html(section(`
			<div style="${CARD}">
				${callout("success", iconRow("success", "Every field that carries data reached ERPNext - nothing was left behind."))}
				${recon ? `<div style="margin-top:8px;">${recon}</div>` : ""}
				${recognizedNote}
				${hrNote}
				${noiseAudit}
			</div>
		`));
		return;
	}

	const unwrittenCount = report.unwritten_field_count || 0;
	const unwrittenNote = unwrittenCount
		? callout(
				"info",
				iconRow(
					"info",
					`<strong>${unwrittenCount}</strong> field(s) were read from the file but
					<strong>not persisted</strong> to ERPNext - worth reviewing, they are a real gap.`
				),
				"margin-bottom:8px;"
		  )
		: "";

	// Lead with the meaningful count when the classifier ran: a file whose unmapped
	// tags are all config constants is not a real loss, and saying "12 not migrated"
	// would alarm without cause. Falls back to the raw count for older logs.
	const meaningfulCount = report.meaningful_unmapped_count;
	const rawUnmapped = report.unmapped_field_count || 0;
	let lead;
	if (meaningfulCount == null) {
		lead = `<strong>${rawUnmapped}</strong> field(s) in your file were
			<strong>not migrated</strong> (Tally custom fields / attributes outside the
			supported mapping). The records themselves still imported.`;
	} else if (meaningfulCount === 0) {
		lead = `The <strong>${rawUnmapped}</strong> unmapped field(s) in your file are all
			Tally config constants (the same value on every record) - <strong>no business
			data</strong>. The records themselves imported in full.`;
	} else {
		const extra = rawUnmapped - meaningfulCount;
		lead = `<strong>${meaningfulCount}</strong> field(s) that look like business data
			were <strong>not migrated</strong> (Tally custom fields outside the supported
			mapping)${extra > 0 ? `, plus ${extra} config constant(s) of no business value` : ""}.
			The records themselves still imported.`;
	}

	wrapper.html(section(`
		<div style="${CARD}">
			<div class="text-muted small" style="margin-bottom:8px;">${lead}</div>
			${unwrittenNote}
			${recon}
			${recognizedNote}
			${hrNote}
			${collapsible(lossTypes.length, `Show field details (${lossTypes.length} record type(s))`, blocks)}
			${noiseAudit}
		</div>
	`));
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

	// A non-zero plug - sometimes large - is expected: it is the amount by which the
	// Tally opening balances do not net to zero, parked in Temporary Opening until the
	// opening entries are finished. Explain it so the figure does not read as an error.
	const plugGross = Number(plug.gross_opening || 0);
	const plugAmt = Number(plug.temporary_opening_plug || 0);
	const plugShare =
		plugGross > 0 ? ` (about ${Math.round((plugAmt / plugGross) * 100)}% of total opening value)` : "";
	const plugLine = plug.clean
		? callout("success", iconRow("success", "Opening balances balanced (Dr = Cr)."), "margin:6px 0;")
		: callout(
				"info",
				iconRow(
					"info",
					`<strong>${fmt(plug.temporary_opening_plug)} ${esc(plug.plug_dr_cr)}${plugShare} is held in Temporary Opening to keep your books balanced.</strong>
					${TEMP_OPENING_NOTE}`
				),
				"margin:6px 0;"
		  );

	const inferredTable = `
		<table class="table table-condensed" style="margin:0;">
			<thead><tr>
				<th style="border-top:0;">Tally ledger</th>
				<th style="border-top:0;">Classified as</th>
				<th style="border-top:0;" class="text-right">Opening</th>
			</tr></thead>
			<tbody>${[...m.inferred]
				.sort((a, b) => (b.amount || 0) - (a.amount || 0))
				.map(
					(r) => `<tr>
						<td>${esc(r.name)}</td>
						<td class="text-muted">${classifiedAs(r)}</td>
						<td class="text-right">${ob(r)}</td>
					</tr>`
				)
				.join("")}</tbody>
		</table>`;

	const inferredBlock = inferred
		? callout(
				"info",
				iconRow(
					"info",
					`<strong>${fmt(inferred)}</strong> account(s) had no standard Tally group - their type was inferred:`
				) + `<div style="margin-top:8px;">${collapsible(inferred, `Show ${fmt(inferred)} inferred account(s)`, inferredTable)}</div>`,
				"margin-top:8px;"
		  )
		: callout(
				"success",
				iconRow("success", `All ${fmt(m.total_accounts)} accounts mapped using Tally's standard groups - none inferred.`),
				"margin-top:8px;"
		  );

	wrapper.html(section(`
		<div style="${CARD}">
			<div class="text-muted small" style="margin-bottom:4px;">
				<strong>${fmt(m.total_accounts)}</strong> ledger account(s) classified -
				<strong>${fmt(confident)}</strong> by Tally's standard groups, <strong>${fmt(inferred)}</strong> inferred.
			</div>
			${plugLine}
			${inferredBlock}
		</div>
	`));
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
		reconciled: { kind: "success", text: "Reconciled - the opening trial balance matches ERPNext." },
		review: { kind: "error", text: "A figure differs between Tally and ERPNext - review the rows below." },
		source_only: { kind: "info", text: "ERPNext figures could not be read back; showing Tally's trial balance only." },
	};
	let v = VERDICT[r.verdict] || VERDICT.source_only;
	// Cumulative-openings heads-up: when this company already holds openings from a
	// *different* export, the ERPNext column is the combined total across all of them,
	// so it cannot line up with this single file. Replace the red "a figure differs"
	// alarm with an informational note - this is expected, not a data error. The
	// backend only sets this flag alongside a real Receivables/Payables divergence,
	// so a genuine single-export mismatch still shows the red alert above.
	if (r.cumulative_openings) {
		const others = (r.other_exports || []).map(esc).join(", ");
		v = {
			kind: "info",
			text:
				"This company already holds opening balances from other imports" +
				(others ? ` (${others})` : "") +
				". The ERPNext column is the company's combined openings across all of " +
				"them, so it will not match this single file - that is expected, not a " +
				"data error. For a clean per-file reconciliation, import each Tally " +
				"export into its own company.",
		};
	}

	const rows = r.rows
		.map((row) => {
			const stat = !row.has_erpnext ? "" : row.match ? statusIcon("success") : statusIcon("error");
			const note = row.is_opening_difference
				? infoTip(`This amount is held in Temporary Opening to keep your books balanced. ${TEMP_OPENING_NOTE}`)
				: "";
			const erpDr = avail && row.has_erpnext ? dr(row.erpnext) : "";
			const erpCr = avail && row.has_erpnext ? cr(row.erpnext) : "";
			const nw = "vertical-align:top; white-space:nowrap;";
			return `
				<tr>
					<td style="font-weight:500; word-break:break-word;"><span style="display:inline-flex; align-items:center; flex-wrap:wrap;">${esc(row.label)}${note}</span></td>
					<td class="text-right" style="${nw}">${dr(row.source)}</td>
					<td class="text-right" style="${nw}">${cr(row.source)}</td>
					<td class="text-right text-muted" style="${nw}">${erpDr}</td>
					<td class="text-right text-muted" style="${nw}">${erpCr}</td>
					<td class="text-center" style="vertical-align:top;">${stat}</td>
				</tr>`;
		})
		.join("");

	const t = r.total || { source: {}, erpnext: {} };
	const bal = (ok) => (ok ? statusIcon("success") : statusIcon("error"));
	const foot = `
		<tr style="border-top:2px solid ${BORDER}; font-weight:500;">
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

	wrapper.html(section(`
		<div style="${CARD} max-width:100%;">
			${callout(v.kind, iconRow(v.kind, v.text), "margin-bottom:8px;")}
			<table class="table table-condensed" style="margin:0; width:100%; table-layout:fixed; font-size:12px;">
				<colgroup>
					<col style="width:24%;">
					<col style="width:16%;"><col style="width:16%;">
					<col style="width:16%;"><col style="width:16%;">
					<col style="width:6%;">
				</colgroup>
				<thead>
					<tr>
						<th style="border-top:0; word-break:break-word;" rowspan="2" class="text-muted">Account class</th>
						<th style="border-top:0;" colspan="2" class="text-center">Tally</th>
						<th style="border-top:0;" colspan="2" class="text-center text-muted">ERPNext</th>
						<th style="border-top:0;" rowspan="2"></th>
					</tr>
					<tr>
						<th class="text-right" style="border-top:0;">Dr</th>
						<th class="text-right" style="border-top:0;">Cr</th>
						<th class="text-right text-muted" style="border-top:0;">Dr</th>
						<th class="text-right text-muted" style="border-top:0;">Cr</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
				<tfoot>${foot}</tfoot>
			</table>
			${stockNote}
		</div>
	`));
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
			const links = names
				.map((item) => {
					// New logs store {name, doctype}; old logs store a bare name string
					// (linked via the label map). A doctype on the item wins, because
					// one label can hold several doctypes (party openings, bank accounts).
					const nm = typeof item === "string" ? item : item.name;
					const dt = (typeof item === "object" && item.doctype) || CREATED_DOCTYPE[label];
					const safe = esc(nm);
					return dt
						? `<a href="/app/${encodeURIComponent(
								frappe.router.slug(dt)
						  )}/${encodeURIComponent(nm)}" target="_blank">${safe}</a>`
						: safe;
				})
				.join(", ");
			// One collapsible per category so each entity's documents fold
			// independently, instead of one giant list of everything.
			return `
				<details style="border-top:1px solid var(--gray-200, #f0f4f7); padding:8px 0;">
					<summary style="cursor:pointer; user-select:none; font-weight:500;">
						${caretMarker()}<span style="margin-left:6px;">${esc(label)} <span class="text-muted" style="font-weight:400;">(${names.length})</span></span>
					</summary>
					<div class="small" style="line-height:1.8; margin:6px 0 0 14px;">${links}</div>
				</details>`;
		})
		.join("");

	wrapper.html(section(`
		<div style="${CARD}">
			<div class="text-muted small" style="margin-bottom:4px;">
				<strong>${total}</strong> ERPNext document(s) were created by this run.
				Use these to review or reverse the migration.
			</div>
			${blocks}
		</div>
	`));
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
				<td class="text-center">${arrowIcon()}</td>
				<td>${e.new ? esc(String(e.new)) : blank}</td>
			</tr>`
		)
		.join("");

	const table = `
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
		</table>`;

	wrapper.html(section(`
		<div style="${CARD}">
			<div class="text-muted small" style="margin-bottom:6px;">
				<strong>${edits.length}</strong> field edit(s) were applied on the pre-flight
				screen before this run. The uploaded file was not modified.
			</div>
			${collapsible(edits.length, `Show all ${edits.length} edit(s)`, table)}
		</div>
	`));
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
		wrapper.html(section(callout("success", iconRow("success", "No data-quality issues were flagged before this run."))));
		return;
	}

	const LABELS = {
		// Errors keep plain problem framing; warnings get the calm outcome-first
		// wording (same map as the Step-3 pre-flight screen).
		GSTIN_INVALID: "Invalid GSTIN",
		ITEM_CODE_COLLISION: "Item code collision",
		GST_STATE_MISSING: "Imports without a GST state",
		GSTIN_STATE_MISMATCH: "GSTIN and state to verify",
		PIN_STATE_CONFLICT: "PIN and state to verify",
		EMAIL_INVALID: "Imports without the email",
		HSN_MISSING: "Imports without HSN",
		DUPLICATE_PARTY: "Possible duplicate to review",
		DUPLICATE_NAME: "Will merge into one",
		CIRCULAR_PARENT: "Hierarchy loop simplified",
	};

	const rows = report.groups
		.map((g) => {
			const isErr = g.severity === "error";
			// Same icon family as everywhere else: red ✕-in-circle for blocking
			// errors, calm blue i-in-circle for non-blocking notices.
			const kind = isErr ? "error" : "info";
			const label = LABELS[g.code] || g.code;
			const items = g.items
				.map(
					(it) =>
						`<div style="padding:2px 0; color:${MUTED};">
							<span class="text-muted">${esc(it.entity_type)}</span> · ${esc(it.entity_name)}
						</div>`
				)
				.join("");
			const head = `<div style="font-weight:500;">${esc(label)}
				<span class="text-muted" style="font-weight:400;">(${g.items.length})</span></div>
				${g.fix_hint ? `<div class="text-muted small" style="margin:2px 0 4px;">${esc(g.fix_hint)}</div>` : ""}
				${collapsible(g.items.length, `Show ${g.items.length} record(s)`, items)}`;
			return `<div style="border-top:1px solid var(--gray-200, #f0f4f7); padding:8px 0;">${iconRow(kind, head)}</div>`;
		})
		.join("");

	wrapper.html(section(`
		<div style="${CARD}">
			<div class="small" style="margin-bottom:6px;">
				<span class="text-danger"><strong>${report.error_group_count ?? report.error_count}</strong> error(s)</span>
				&nbsp;·&nbsp;
				<span style="color:var(--blue-600, #318ad8);"><strong>${report.warning_group_count ?? report.warning_count}</strong> warning(s)</span>
				<span class="text-muted">- flagged before this run</span>
			</div>
			${rows}
		</div>
	`));
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
			section(
				`<div class="text-muted" style="padding:6px 0;">
					${frm.doc.status === "Running"
						? "Migration is still running..."
						: "No import summary recorded for this run."}
				</div>`
			)
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
				<div style="display:flex; height:8px; border-radius:4px; overflow:hidden; background:var(--gray-200, #f0f4f7);">
					<div style="width:${pct(created)}%; background:${BAR_CREATED};"></div>
					<div style="width:${pct(skipped)}%; background:${BAR_SKIPPED};"></div>
					<div style="width:${pct(failed)}%; background:${BAR_FAILED};"></div>
				</div>`;

			return `
				<tr>
					<td style="font-weight:500; white-space:nowrap; vertical-align:middle;">${esc(label)}</td>
					<td style="width:45%; vertical-align:middle;">${bar}</td>
					<td class="text-right text-success" style="vertical-align:middle;">${created}</td>
					<td class="text-right text-muted" style="vertical-align:middle;">${skipped}</td>
					<td class="text-right ${failed ? "text-danger" : "text-muted"}" style="vertical-align:middle;">
						${failed ? `<strong>${failed}</strong>` : failed}
					</td>
				</tr>`;
		})
		.join("");

	// Legend swatches reuse the exact bar fills - they can never drift apart.
	// Each item is its own flex pair so the chip is optically centred on the text.
	const legendItem = (color, text) =>
		`<span style="display:inline-flex; align-items:center; gap:6px;">
			<span style="width:10px; height:10px; border-radius:2px; background:${color};"></span>${text}</span>`;

	wrapper.html(section(`
		<div style="${CARD}">
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
					<tr style="border-top:2px solid ${BORDER};">
						<td style="font-weight:500;">Total</td>
						<td></td>
						<td class="text-right text-success"><strong>${totalCreated}</strong></td>
						<td class="text-right text-muted">${totalSkipped}</td>
						<td class="text-right ${totalFailed ? "text-danger" : "text-muted"}">
							${totalFailed ? `<strong>${totalFailed}</strong>` : totalFailed}
						</td>
					</tr>
				</tfoot>
			</table>
			<div class="text-muted small" style="margin-top:10px; display:flex; flex-wrap:wrap; align-items:center; gap:6px 16px;">
				${legendItem(BAR_CREATED, "Imported (new)")}
				${legendItem(BAR_SKIPPED, "Already there (skipped, safe)")}
				${legendItem(BAR_FAILED, "Failed")}
			</div>
		</div>
	`));
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
	frappe.dom.freeze(__("Re-running migration..."));
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

/* ============================================================================
 * ROLLBACK FEATURE (optional - "Undo This Migration")
 * ---------------------------------------------------------------------------
 * Self-contained: this whole block plus the single tally_rollback_attach(frm)
 * call in refresh() is the entire client footprint. Delete both to remove the
 * feature's UI. The button appears on any saved migration that created records
 * and has not already been reverted.
 * ========================================================================== */
function tally_rollback_attach(frm) {
	if (frm.is_new()) return;
	// Nothing to undo unless the run recorded created documents and hasn't
	// already been reverted.
	let created = {};
	try {
		created = JSON.parse(frm.doc.created_records || "{}");
	} catch (e) {
		created = {};
	}
	const total = Object.values(created).reduce((n, names) => n + (names || []).length, 0);
	// A clean undo flips the log to "Reverted" and the action is withdrawn. One that
	// kept records becomes "Reverted with Errors" and stays offered, relabelled, so the
	// user can retry after fixing the cause (the re-run is idempotent server-side).
	if (!total || frm.doc.status === "Reverted") return;
	const retry = frm.doc.status === "Reverted with Errors";

	// Tucked inside the standard "Actions" menu rather than a loud standalone
	// button - it's a rare, destructive operation.
	frm.add_custom_button(
		retry ? __("Retry Undo (records remained)") : __("Undo This Migration"),
		() => tally_rollback_confirm(frm, total),
		__("Actions")
	);
}

function tally_rollback_confirm(frm, total) {
	const company = frm.doc.company || "";
	// Pull a server-authoritative breakdown of what will be deleted, so the dialog
	// shows "12 Item, 5 Customer, 3 Journal Entry..." rather than a bare count. Purely
	// read-only; nothing is deleted until the user confirms below.
	frappe.call({
		method: "tally_migrator.migration.rollback.preview_revert",
		args: { log_name: frm.doc.name },
		callback: (r) => tally_rollback_dialog(frm, company, r.message || { total }),
	});
}

function tally_rollback_dialog(frm, company, preview) {
	const total = preview.total || 0;
	const breakdown = Object.entries(preview.by_doctype || {})
		.map(
			([dt, n]) =>
				`<li><strong>${n}</strong> ${frappe.utils.escape_html(
					__(dt)
				)}${n === 1 ? "" : "s"}</li>`
		)
		.join("");
	const d = new frappe.ui.Dialog({
		title: __("Undo This Migration"),
		fields: [
			{
				fieldtype: "HTML",
				options: `
					<div style="font-size:13px; line-height:1.6;">
						<p>This will <strong>permanently delete</strong> the
						<strong>${total}</strong> ERPNext document(s) this import created -
						including any opening entries, invoices and stock reconciliations.</p>
						${
							breakdown
								? `<ul style="padding-left:18px; columns:2; color:var(--text-muted); margin-bottom:8px;">${breakdown}</ul>`
								: ""
						}
						<ul style="padding-left:18px; color:var(--text-muted);">
							<li>Submitted entries are cancelled first, then deleted.</li>
							<li>Anything now linked to activity created <em>after</em> this
								migration is <strong>kept</strong> and listed for you.</li>
							<li>Only this migration's records are touched - nothing else in
								the company.</li>
						</ul>
						<p>To confirm, type the company name
						<strong>${frappe.utils.escape_html(company)}</strong> below.</p>
					</div>`,
			},
			{
				fieldname: "company_confirmation",
				fieldtype: "Data",
				label: __("Company name"),
				reqd: 1,
			},
		],
		primary_action_label: __("Delete migrated records"),
		primary_action: (values) => {
			if ((values.company_confirmation || "").trim() !== company.trim()) {
				frappe.msgprint(__("The company name does not match. Nothing was deleted."));
				return;
			}
			d.hide();
			tally_rollback_run(frm, values.company_confirmation);
		},
	});
	d.show();
	// Make the confirm button read as destructive.
	d.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
}

function tally_rollback_run(frm, company_confirmation) {
	frappe.dom.freeze(__("Queueing undo…"));
	frappe.call({
		method: "tally_migrator.migration.rollback.revert_migration",
		args: { log_name: frm.doc.name, company_confirmation },
		callback: (r) => {
			frappe.dom.unfreeze();
			const res = r.message || {};
			if (!res.revert) return;
			// The deletion runs in a background job. Hand the user a link to the
			// Revert record where they can watch status and see the per-document
			// result table (mirrors ERPNext's Transaction Deletion Record).
			const link = frappe.utils.get_form_link(
				"Tally Migration Revert", res.revert, true, res.revert
			);
			frappe.msgprint({
				title: __("Undo started"),
				indicator: "blue",
				message: __("The undo is running in the background. Track its progress and see what was deleted or kept here: {0}.", [link]),
			});
		},
		error: () => frappe.dom.unfreeze(),
	});
}
/* ===== END ROLLBACK FEATURE ===== */
