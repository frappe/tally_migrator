frappe.pages["tally-migrator"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Tally Migrator",
		single_column: true,
	});

	new TallyMigratorPage(page, wrapper);
};

const STEPS = [
	{ id: "section-upload", label: "Upload" },
	{ id: "section-configure", label: "Configure" },
	{ id: "section-check", label: "Check" },
	{ id: "section-run", label: "Migrate" },
];

class TallyMigratorPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.fileUrl = null;
		this.fileName = null;
		this.preview = null;       // {customers, suppliers, items, warehouses}
		this.uomIssues = [];       // [{tally_uom, erpnext_uom, exists}]
		this.allUoms = [];         // existing ERPNext UOM names
		this.uomOverrides = {};    // {tally_uom: final_erpnext_uom} after resolution
		this.qualityReport = null; // grouped data-quality report from the pre-flight scan
		this.recordOverrides = {}; // {entityType: {name: {field: value}}} inline fixes
		this.states = [];          // ERPNext state names for the inline state dropdown
		this.render();
	}

	render() {
		$(this.wrapper).find(".page-content").html(`
			<div class="container" style="max-width:680px; padding-top: 24px;">

				<!-- Persistent stepper -->
				<div id="stepper" style="display:flex; align-items:center; margin-bottom:28px;"></div>

				<!-- STEP 1: Upload -->
				<div id="section-upload">
					<h4>Bring your Tally data into ERPNext</h4>
					<p class="text-muted" style="margin-bottom:18px;">
						This tool copies your master records — Customers, Suppliers, Items and Warehouses —
						from Tally into ERPNext. It takes a few short steps.
					</p>

					<div class="alert alert-info" style="display:flex; gap:10px; align-items:flex-start;">
						<span style="font-size:16px;">🛡️</span>
						<div style="font-size:13px;">
							<strong>Your existing ERPNext data is safe.</strong>
							Nothing is ever overwritten or deleted. If a record already exists, the migrator
							skips it. You can run this as many times as you like.
						</div>
					</div>

					<div class="well well-sm" style="margin-top:18px;">
						<strong>First, export a file from Tally</strong>
						<ol style="margin:10px 0 0 0; padding-left:20px; font-size:13px; line-height:1.7;">
							<li>Open your company in <strong>Tally Prime</strong>.</li>
							<li>Go to <strong>Gateway of Tally → Import/Export → Export</strong>.</li>
							<li>Choose <strong>Masters</strong> as the type.</li>
							<li>Set <strong>Format</strong> to <strong>XML</strong>, and <strong>Show All Masters</strong> to <strong>Yes</strong>.</li>
							<li>Export, and note where the <code>.xml</code> file is saved.</li>
						</ol>
					</div>

					<div style="margin-top:18px;">
						<strong>Then upload it here</strong>
						<div style="margin-top:8px;">
							<button id="btn-pick-file" class="btn btn-default btn-sm">
								<i class="fa fa-upload"></i> &nbsp;Choose Tally XML file
							</button>
							<span id="file-status" style="margin-left:12px;" class="text-muted"></span>
						</div>
					</div>

					<!-- Preview of what's inside the file -->
					<div id="preview-box" style="display:none; margin-top:18px;"></div>

					<div style="margin-top:24px;">
						<button id="btn-next-upload" class="btn btn-primary btn-sm" disabled>Continue →</button>
					</div>
				</div>

				<!-- STEP 2: Configure -->
				<div id="section-configure" style="display:none;">
					<h4>Choose where the data should go</h4>
					<p class="text-muted">Select the ERPNext company that will receive these records.</p>

					<div class="form-group">
						<label class="control-label">ERPNext Company</label>
						<select id="erpnext-company" class="form-control" style="max-width:360px;"></select>
						<div id="company-empty" style="display:none; margin-top:8px;" class="text-muted small">
							No company found. <a href="/app/company/new">Create a Company in ERPNext</a> first,
							then come back and refresh this page.
						</div>
					</div>

					<div class="row">
						<div class="form-group col-sm-6">
							<label class="control-label">Chart of Accounts</label>
							<select id="coa-mode" class="form-control" style="max-width:360px;">
								<option value="reuse">Reuse ERPNext's standard accounts (recommended)</option>
								<option value="mirror">Mirror Tally's group tree exactly</option>
							</select>
							<div class="text-muted small" style="margin-top:4px;">
								<span id="coa-mode-hint">Tally's reserved groups map onto ERPNext's
								built-in Chart of Accounts; only your custom groups and ledgers are created.</span>
							</div>
						</div>
						<div class="form-group col-sm-6">
							<label class="control-label">Opening-balance date</label>
							<input type="date" id="opening-date" class="form-control" style="max-width:360px;" />
							<div class="text-muted small" style="margin-top:4px;">
								Posting date for opening balances &amp; stock. Leave blank to use the
								company's current fiscal-year start.
							</div>
						</div>
					</div>

					<div class="well well-sm" style="margin-top:16px; margin-bottom:20px;">
						<strong>Here's what will be imported</strong>
						<div id="configure-counts" style="margin-top:10px;"></div>
					</div>

					<button id="btn-back-2" class="btn btn-default btn-sm">← Back</button>
					&nbsp;
					<button id="btn-next-2" class="btn btn-primary btn-sm">Continue →</button>
				</div>

				<!-- STEP 3: Pre-flight check -->
				<div id="section-check" style="display:none;">
					<h4>Quick check before we begin</h4>
					<p class="text-muted">
						We compare the data in your file against what already exists in ERPNext, so you can
						decide how to handle anything that doesn't match — nothing is changed automatically.
					</p>

					<div id="check-loading" class="text-muted" style="margin:18px 0;">
						<i class="fa fa-spinner fa-spin"></i> &nbsp;Checking your file against ERPNext…
					</div>

					<div id="check-clean" style="display:none;" class="alert alert-success">
						<strong>✓ Nothing to resolve.</strong> Everything in your file matches what ERPNext expects.
					</div>

					<!-- Data-quality report (read-only; informational + consent) -->
					<div id="dq-section" style="display:none; margin-bottom:18px;">
						<div id="dq-cards" style="display:flex; gap:10px; margin-bottom:12px;"></div>
						<div id="dq-list"></div>
						<div id="dq-consent" style="display:none; margin-top:12px;" class="alert alert-danger">
							<label style="margin:0; font-weight:400; cursor:pointer;">
								<input type="checkbox" id="dq-consent-check" />
								&nbsp;I understand some records have errors and will fail to import. Continue and migrate the rest.
							</label>
						</div>
					</div>

					<!-- Company-readiness gate (blockers stop the run) -->
					<div id="readiness-section" style="display:none; margin-bottom:18px;"></div>

					<!-- Field-coverage notice (read-only; informational) -->
					<div id="coverage-section" style="display:none; margin-bottom:18px;"></div>

					<div id="check-issues" style="display:none;">
						<div class="alert alert-warning" style="margin-bottom:14px;">
							<strong>⚠ Some Units of Measure in your file don't exist in ERPNext yet.</strong>
							By default we'll create each one as a new unit. Change any row below if you'd
							rather map it to a unit you already use — then click Continue.
						</div>
						<div id="uom-issue-list"></div>
					</div>

					<div style="margin-top:24px;">
						<button id="btn-back-check" class="btn btn-default btn-sm">← Back</button>
						&nbsp;
						<button id="btn-next-check" class="btn btn-primary btn-sm">Continue →</button>
					</div>
				</div>

				<!-- STEP 4: Run & Results -->
				<div id="section-run" style="display:none;">
					<h4>Migration</h4>
					<p id="run-subtitle" class="text-muted"></p>

					<div id="progress-section" style="display:none; margin-bottom:20px;">
						<div class="progress" style="margin-bottom:6px;">
							<div id="progress-bar" class="progress-bar progress-bar-striped active" style="width:0%; min-width:2em;">0%</div>
						</div>
						<p id="progress-desc" class="text-muted" style="font-size:12px; margin:0;">Starting…</p>
					</div>

					<div id="results-section" style="display:none;"></div>

					<div id="error-section" style="display:none;" class="alert alert-danger"></div>

					<div id="run-actions">
						<button id="btn-back-3" class="btn btn-default btn-sm">← Back</button>
						&nbsp;
						<button id="btn-run" class="btn btn-primary btn-sm">▶ Run Migration</button>
					</div>
				</div>

			</div>
		`);

		this.renderStepper("section-upload");
		this.bindEvents();
	}

	// ── Persistent stepper ──────────────────────────────────────────────────────
	// Frappe default (near-black) for the active step, green for completed,
	// light grey for steps still ahead — matches standard Frappe desk styling.

	renderStepper(activeId) {
		const activeIdx = STEPS.findIndex((s) => s.id === activeId);
		const ACTIVE = "#1f272e";   // Frappe desk default text/ink color
		const DONE = "#28a745";     // success green
		const PENDING = "#d1d8dd";  // light grey

		const parts = STEPS.map((s, i) => {
			const done = i < activeIdx;
			const active = i === activeIdx;
			const circleColor = done ? DONE : active ? ACTIVE : PENDING;
			const textColor = active ? ACTIVE : done ? DONE : "#8d99a6";
			const circle = `
				<div style="display:flex; align-items:center; gap:8px;">
					<span style="display:inline-flex; align-items:center; justify-content:center;
						width:24px; height:24px; border-radius:50%; background:${circleColor};
						color:#fff; font-size:12px; font-weight:600;">
						${done ? "✓" : i + 1}
					</span>
					<span style="color:${textColor}; font-weight:${active ? 600 : 400}; font-size:13px;">${s.label}</span>
				</div>`;
			const connector =
				i < STEPS.length - 1
					? `<div style="flex:1; height:2px; background:${i < activeIdx ? DONE : "#e0e6ed"}; margin:0 12px;"></div>`
					: "";
			return circle + connector;
		});
		$("#stepper").html(parts.join(""));
	}

	bindEvents() {
		// Step 1 — upload + advance
		$("#btn-pick-file").on("click", () => this.pickFile());
		$("#btn-next-upload").on("click", () => {
			if (!this.fileUrl) {
				frappe.msgprint("Please upload a Tally XML file first.");
				return;
			}
			this.proceedToConfigure();
		});

		// Step 2
		$("#coa-mode").on("change", function () {
			$("#coa-mode-hint").text(
				$(this).val() === "mirror"
					? "Every Tally group is recreated verbatim in ERPNext, preserving your exact tree."
					: "Tally's reserved groups map onto ERPNext's built-in Chart of Accounts; only your custom groups and ledgers are created."
			);
		});
		$("#btn-back-2").on("click", () => this.show("section-upload"));
		$("#btn-next-2").on("click", () => {
			const erpnext = $("#erpnext-company").val();
			if (!erpnext) {
				frappe.msgprint("Please select an ERPNext company.");
				return;
			}
			this.proceedToCheck();
		});

		// Step 3 — pre-flight check
		$("#btn-back-check").on("click", () => this.show("section-configure"));
		$("#btn-next-check").on("click", () => {
			if (this.readiness && this.readiness.ready === false) {
				frappe.msgprint(
					__("This company isn't ready to receive masters. Resolve the blockers shown above, then Re-check.")
				);
				return;
			}
			this.resolveUomsAndContinue();
		});

		// Step 4
		$("#btn-back-3").on("click", () => this.show("section-check"));
		$("#btn-run").on("click", () => this.runMigration());
	}

	// ── Step 1: upload + preview ────────────────────────────────────────────────

	pickFile() {
		new frappe.ui.FileUploader({
			folder: "Home/Attachments",
			restrictions: { allowed_file_types: [".xml", "text/xml", "application/xml"] },
			on_success: (file_doc) => {
				this.fileUrl = file_doc.file_url;
				this.fileName = file_doc.file_name || file_doc.file_url;
				$("#file-status").html(
					`<span class="indicator green">${frappe.utils.escape_html(this.fileName)}</span>`
				);
				this.loadPreview();
			},
		});
	}

	loadPreview() {
		$("#preview-box")
			.show()
			.html(`<span class="text-muted"><i class="fa fa-spinner fa-spin"></i> &nbsp;Reading your file…</span>`);
		$("#btn-next-upload").prop("disabled", true);

		frappe.call({
			method: "tally_migrator.api.preview_masters_file",
			args: { file_url: this.fileUrl },
			callback: (r) => {
				const p = r.message || {};
				this.preview = p;
				const total =
					(p.customers || 0) + (p.suppliers || 0) + (p.items || 0) + (p.warehouses || 0);
				if (total === 0) {
					$("#preview-box").html(
						`<div class="alert alert-warning" style="margin:0;">
							We read the file, but found no Customers, Suppliers, Items or Warehouses in it.
							Make sure you exported <strong>Masters</strong> (with <strong>Show All Masters = Yes</strong>) from Tally.
						</div>`
					);
					$("#btn-next-upload").prop("disabled", true);
					return;
				}
				$("#preview-box").html(
					`<div class="alert alert-success" style="margin:0;">
						<strong>✓ File read successfully.</strong> Here's what we found:
						${this.countsHtml(p)}
					</div>`
				);
				$("#btn-next-upload").prop("disabled", false);
			},
			error: () => {
				$("#preview-box").html(
					`<div class="alert alert-danger" style="margin:0;">
						We couldn't read this file. Please make sure it's a valid Tally <strong>Masters XML</strong>
						export and upload it again.
					</div>`
				);
				$("#btn-next-upload").prop("disabled", true);
			},
		});
	}

	countsHtml(p) {
		const accounts = (p.account_groups || 0) + (p.ledger_accounts || 0);
		const rows = [
			["Customers", p.customers || 0, true],
			["Suppliers", p.suppliers || 0, true],
			["Items", p.items || 0, true],
			["Warehouses", p.warehouses || 0, true],
			// COA entities only show when present (many files are masters-only).
			["Accounts", accounts, accounts > 0],
			["Cost Centres", p.cost_centres || 0, (p.cost_centres || 0) > 0],
		];
		const chips = rows
			.filter(([, , show]) => show)
			.map(
				([label, n]) =>
					`<span style="display:inline-block; margin:6px 8px 0 0; padding:3px 10px;
						background:#fff; border:1px solid #d1d8dd; border-radius:12px; font-size:12px;">
						<strong>${n}</strong> ${label}</span>`
			)
			.join("");
		return `<div style="margin-top:6px;">${chips}</div>`;
	}

	proceedToConfigure() {
		this.loadERPNextCompanies();
		$("#configure-counts").html(this.preview ? this.countsHtml(this.preview) : "");
		this.show("section-configure");
	}

	// ── Navigation ──────────────────────────────────────────────────────────────

	show(sectionId) {
		STEPS.forEach((s) => $("#" + s.id).hide());
		$("#" + sectionId).show();
		this.renderStepper(sectionId);
	}

	loadERPNextCompanies() {
		frappe.call({
			method: "frappe.client.get_list",
			args: { doctype: "Company", fields: ["name"], limit_page_length: 100 },
			callback: (r) => {
				const companies = r.message || [];
				const $select = $("#erpnext-company").empty();
				if (!companies.length) {
					$("#company-empty").show();
					$("#btn-next-2").prop("disabled", true);
					return;
				}
				$("#company-empty").hide();
				$("#btn-next-2").prop("disabled", false);
				$select.append('<option value="">Select company…</option>');
				companies.forEach((c) => {
					const name = frappe.utils.escape_html(c.name);
					$select.append(`<option value="${name}">${name}</option>`);
				});
				// Auto-select when there is exactly one.
				if (companies.length === 1) $select.val(companies[0].name);
			},
		});
	}

	// ── Step 3: pre-flight check ─────────────────────────────────────────────────

	proceedToCheck() {
		this.uomIssues = [];
		this.allUoms = [];
		this.uomOverrides = {};
		this.qualityReport = null;
		this.coverageReport = null;
		this.readiness = null;
		this.recordOverrides = {};
		this.states = [];

		$("#check-loading").show();
		$("#check-clean").hide();
		$("#check-issues").hide();
		$("#dq-section").hide();
		$("#readiness-section").hide();
		$("#coverage-section").hide();
		this.show("section-check");

		// Two independent read-only scans run in parallel: data-quality (GST / HSN /
		// duplicates / collisions) and UOM resolution. Render once both return.
		let pending = 2;
		const done = () => {
			if (--pending > 0) return;
			$("#check-loading").hide();
			const noUom = !this.uomIssues.length;
			const noDq = !this.qualityReport || this.qualityReport.clean;
			if (noUom && noDq) {
				$("#check-clean").show();
			}
		};

		frappe.call({
			method: "tally_migrator.api.validate_masters_data",
			args: { file_url: this.fileUrl, erpnext_company: $("#erpnext-company").val() },
			callback: (r) => {
				this.qualityReport = r.message || null;
				this.states = (r.message && r.message.states) || [];
				this.coverageReport = (r.message && r.message.coverage) || null;
				this.readiness = (r.message && r.message.readiness) || null;
				this.renderDataQuality();
				this.renderReadiness();
				this.renderCoverage();
				done();
			},
			error: () => done(),  // non-fatal — importer still reports failures later
		});

		frappe.call({
			method: "tally_migrator.api.validate_masters_file",
			args: { file_url: this.fileUrl },
			callback: (r) => {
				const data = r.message || {};
				const issues = (data.issues || []).filter((i) => !i.exists);
				this.uomIssues = issues;
				this.allUoms = data.all_uoms || [];
				if (issues.length) {
					$("#check-issues").show();
					this.renderUomIssues();
				}
				done();
			},
			error: () => done(),
		});
	}

	// Company-readiness gate. Blockers (a whole entity would fail) disable
	// Continue; warnings (partial degradation) are shown but don't block.
	renderReadiness() {
		const report = this.readiness;
		const $sec = $("#readiness-section");
		const $btn = $("#btn-next-check");
		if (!report || (report.ready && !(report.warnings || []).length)) {
			$sec.hide().empty();
			$btn.prop("disabled", false);
			return;
		}
		const esc = frappe.utils.escape_html;
		const row = (it, color) => `
			<div style="padding:4px 0; border-top:1px solid rgba(0,0,0,0.06);">
				<div style="font-weight:600; color:${color};">${esc(it.message)}</div>
				<div class="text-muted small">Fix: ${esc(it.fix)}</div>
			</div>`;
		const blockers = (report.blockers || []).map((b) => row(b, "#c0392b")).join("");
		const warnings = (report.warnings || []).map((w) => row(w, "#b8860b")).join("");

		const hasBlockers = (report.blockers || []).length > 0;
		const cls = hasBlockers ? "alert-danger" : "alert-warning";
		const head = hasBlockers
			? `<strong>✋ This company isn't ready — fix the items below before migrating.</strong>`
			: `<strong>⚠ This company can receive masters, but some steps are degraded.</strong>`;

		$sec.html(`
			<div class="alert ${cls}" style="margin:0;">
				${head}
				${blockers ? `<div style="margin-top:10px;">${blockers}</div>` : ""}
				${warnings ? `<div style="margin-top:10px;">${warnings}</div>` : ""}
				<div style="margin-top:10px;">
					<button class="btn btn-xs btn-default" id="btn-recheck-readiness">Re-check</button>
				</div>
			</div>
		`).show();

		$("#btn-recheck-readiness").on("click", () => this.recheckReadiness());
		$btn.prop("disabled", hasBlockers);
	}

	// Re-run only the readiness check (after the user fixes setup in another tab),
	// without re-scanning the whole file.
	recheckReadiness() {
		frappe.call({
			method: "tally_migrator.api.company_readiness",
			args: { erpnext_company: $("#erpnext-company").val() },
			callback: (r) => {
				this.readiness = r.message || null;
				this.renderReadiness();
			},
		});
	}

	// Read-only notice: fields present in the file that we do NOT migrate (Tally
	// UDFs / unmapped attributes). Informational — it never blocks Continue.
	renderCoverage() {
		const report = this.coverageReport;
		const $sec = $("#coverage-section");
		if (!report || report.clean || !(report.types || []).length) {
			$sec.hide().empty();
			return;
		}
		const esc = frappe.utils.escape_html;
		const row = (u, status) => `<tr>
				<td style="font-family:monospace;">${esc(u.field)}</td>
				<td class="text-right text-muted">${u.count}</td>
				<td class="text-muted">${u.sample ? esc(String(u.sample)) : ""}</td>
				<td class="text-muted">${status}</td>
			</tr>`;
		const blocks = report.types
			.map((t) => {
				// Both kinds are reported: "Not mapped" (a Tally custom field we never
				// read) and "Read, not imported" (a field we parse but no importer
				// persists). The second used to be hidden, making the count read 0.
				const rows = [
					...(t.unmapped || []).map((u) => row(u, "Not mapped")),
					...(t.unwritten || []).map((u) => row(u, "Read, not imported")),
				].join("");
				if (!rows) return "";
				return `
					<div style="margin-bottom:8px;">
						<div style="font-weight:600; margin-bottom:3px;">${esc(t.entity_type)}</div>
						<table class="table table-condensed" style="margin:0;">
							<thead><tr>
								<th style="border-top:0;">Field</th>
								<th style="border-top:0;" class="text-right">Count</th>
								<th style="border-top:0;">Sample value</th>
								<th style="border-top:0;">Status</th>
							</tr></thead>
							<tbody>${rows}</tbody>
						</table>
					</div>`;
			})
			.join("");
		const total =
			(report.unmapped_field_count || 0) + (report.unwritten_field_count || 0);
		$sec.html(`
			<div class="alert alert-info" style="margin:0;">
				<strong>ℹ ${total} field(s) in your file won't be migrated.</strong>
				These are either Tally custom fields outside the supported mapping
				("Not mapped"), or fields we read but don't import ("Read, not imported").
				Your records will still import — this is just so nothing is dropped without
				you knowing. A copy is saved on the migration log for your records.
				<div style="margin-top:10px;">${blocks}</div>
			</div>
		`).show();
	}

	static get DQ_LABELS() {
		return {
			GSTIN_INVALID: "Invalid GSTIN",
			GST_STATE_MISSING: "GST state missing",
			GSTIN_STATE_MISMATCH: "GSTIN / state mismatch",
			PIN_STATE_CONFLICT: "PIN / state conflict",
			HSN_MISSING: "HSN code missing",
			ITEM_CODE_COLLISION: "Item code collision",
			DUPLICATE_PARTY: "Possible duplicate party",
			DUPLICATE_NAME: "Duplicate name (will merge)",
			CIRCULAR_PARENT: "Circular parent hierarchy",
		};
	}

	// Render the grouped data-quality report: stat cards + one expandable row per
	// rule code. Editable rules show inline inputs (pre-filled) so the user can fix
	// flagged fields; "Re-check" re-validates the fixes against the same engine.
	// Edits never touch the source file — they ride along as in-memory overrides.
	// Errors that remain gate Continue via an explicit consent checkbox.
	renderDataQuality() {
		const report = this.qualityReport;
		if (!report) {
			$("#dq-section").hide();
			return;
		}
		// Clean now: if the user fixed everything, say so; otherwise stay hidden.
		if (report.clean || !(report.groups || []).length) {
			if (Object.keys(this.recordOverrides).length) {
				$("#dq-cards").empty();
				$("#dq-list").html(
					`<div class="alert alert-success" style="margin:0;">✓ All flagged data issues are resolved.</div>`
				);
				$("#dq-consent").hide();
				$("#btn-next-check").prop("disabled", false);
				$("#dq-section").show();
			} else {
				$("#dq-section").hide();
			}
			return;
		}

		const esc = frappe.utils.escape_html;
		const card = (n, label, color) => `
			<div style="flex:1; border:1px solid #e0e6ed; border-radius:6px; padding:10px 12px; text-align:center;">
				<div style="font-size:20px; font-weight:700; color:${color};">${n}</div>
				<div class="text-muted small">${label}</div>
			</div>`;
		$("#dq-cards").html(
			card(report.error_count, "Errors", report.error_count ? "#e24c4c" : "#8d99a6") +
			card(report.warning_count, "Warnings", report.warning_count ? "#f0a500" : "#8d99a6")
		);

		const rows = report.groups.map((g, idx) => this.dqGroupHtml(g, idx)).join("");
		const hasEditable = report.groups.some((g) => (g.editable_fields || []).length);
		const toolbar = hasEditable
			? `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
					<span class="text-muted small">Fix a value below, then re-check — or continue anyway.</span>
					<button class="btn btn-default btn-xs" id="btn-dq-recheck">↻ Re-check</button>
				</div>`
			: "";

		$("#dq-list").html(`
			${toolbar}
			<div style="border:1px solid #e0e6ed; border-radius:6px; padding:6px 14px; max-height:340px; overflow-y:auto;">
				${rows}
			</div>`);

		$("#dq-list .dq-head").on("click", (e) => {
			const idx = $(e.currentTarget).data("idx");
			const $body = $("#dq-body-" + idx);
			$body.toggle();
			$("#dq-caret-" + idx).text($body.is(":visible") ? "▾" : "▸");
		});
		$("#dq-list .dq-edit").on("change", (e) => this.captureEdit(e.currentTarget));
		$("#btn-dq-recheck").on("click", () => this.recheck());

		// Errors require explicit consent before Continue.
		if (report.error_count > 0) {
			$("#dq-consent").show();
			$("#btn-next-check").prop("disabled", true);
			$("#dq-consent-check").prop("checked", false).off("change").on("change", (e) => {
				$("#btn-next-check").prop("disabled", !e.target.checked);
			});
		} else {
			$("#dq-consent").hide();
			$("#btn-next-check").prop("disabled", false);
		}
		$("#dq-section").show();
	}

	// One expandable group: header + fix hint + per-record rows (with inline editors
	// when the rule is fixable).
	dqGroupHtml(g, idx) {
		const esc = frappe.utils.escape_html;
		const dot = g.severity === "error" ? "#e24c4c" : "#f0a500";
		const label = TallyMigratorPage.DQ_LABELS[g.code] || g.code;
		const editable = g.editable_fields || [];
		const items = g.items.map((it) => this.dqItemHtml(it, editable)).join("");
		return `
			<div style="border-top:1px solid #f0f4f7;">
				<div class="dq-head" data-idx="${idx}" style="cursor:pointer; padding:8px 0; display:flex; align-items:center; gap:6px;">
					<span style="color:${dot};">■</span>
					<strong>${esc(label)}</strong>
					<span class="text-muted">(${g.items.length})</span>
					<span class="text-muted" style="margin-left:auto;" id="dq-caret-${idx}">▸</span>
				</div>
				${g.fix_hint ? `<div class="text-muted small" style="margin:-2px 0 6px;">${esc(g.fix_hint)}</div>` : ""}
				<div class="dq-body" id="dq-body-${idx}" style="display:none; margin:0 0 8px 16px;">${items}</div>
			</div>`;
	}

	dqItemHtml(it, editableFields) {
		const esc = frappe.utils.escape_html;
		const name = `<div style="padding:2px 0; color:#555;">
			<span class="text-muted">${esc(it.entity_type)}</span> · ${esc(it.entity_name)}
		</div>`;
		if (!editableFields.length) return name;
		const inputs = editableFields.map((f) => this.dqFieldHtml(it, f)).join("");
		return `<div style="padding:4px 0;">
			${name}
			<div style="display:flex; flex-wrap:wrap; gap:8px; margin:2px 0 6px 12px;">${inputs}</div>
		</div>`;
	}

	dqFieldHtml(it, f) {
		const esc = frappe.utils.escape_html;
		const cur = this.overrideValue(it, f.field);
		const attrs = `class="form-control input-sm dq-edit" style="width:auto; min-width:160px; display:inline-block;"
			data-etype="${esc(it.entity_type)}" data-name="${esc(it.entity_name)}" data-field="${esc(f.field)}"`;
		const lbl = `<span class="text-muted small" style="margin-right:4px;">${esc(f.label)}:</span>`;
		if (f.type === "state") {
			const opts = ['<option value="">— select —</option>']
				.concat(this.states.map((s) => `<option value="${esc(s)}" ${s === cur ? "selected" : ""}>${esc(s)}</option>`))
				.join("");
			return `<label style="margin:0; font-weight:400;">${lbl}<select ${attrs}>${opts}</select></label>`;
		}
		return `<label style="margin:0; font-weight:400;">${lbl}<input type="text" ${attrs} value="${esc(cur)}" placeholder="${esc(f.label)}"></label>`;
	}

	// Prefer an edit the user already made this session; fall back to the file value.
	overrideValue(it, field) {
		const byType = this.recordOverrides[it.entity_type] || {};
		const byName = byType[it.entity_name] || {};
		if (field in byName) return byName[field];
		return (it.current && it.current[field]) || "";
	}

	captureEdit(el) {
		const $el = $(el);
		const etype = $el.data("etype");
		const name = $el.data("name");
		const field = $el.data("field");
		this.recordOverrides[etype] = this.recordOverrides[etype] || {};
		this.recordOverrides[etype][name] = this.recordOverrides[etype][name] || {};
		this.recordOverrides[etype][name][field] = $el.val();
	}

	// Re-validate with the in-memory edits applied — fixes are confirmed by the same
	// engine, so resolved issues drop off and any remaining ones stay visible.
	recheck() {
		frappe.dom.freeze(__("Re-checking…"));
		frappe.call({
			method: "tally_migrator.api.validate_masters_data",
			args: {
				file_url: this.fileUrl,
				record_overrides: JSON.stringify(this.recordOverrides),
				// Pass the company so the readiness panel is recomputed alongside the
				// data fixes, instead of showing a stale readiness state.
				erpnext_company: $("#erpnext-company").val() || "",
			},
			callback: (r) => {
				frappe.dom.unfreeze();
				this.qualityReport = r.message || null;
				this.states = (r.message && r.message.states) || this.states;
				if (r.message && r.message.readiness) {
					this.readiness = r.message.readiness;
					this.renderReadiness();
				}
				this.renderDataQuality();
			},
			error: () => frappe.dom.unfreeze(),
		});
	}

	// Compact, scalable table: one row per missing unit. Every row defaults to
	// "create as new", so 3 or 300 issues both resolve in a single Continue
	// click; per-row dropdowns let the user map specific units to existing ones.
	renderUomIssues() {
		const esc = frappe.utils.escape_html;
		const existingOptions = this.allUoms
			.map((u) => `<option value="${esc(u)}">Map to existing: ${esc(u)}</option>`)
			.join("");

		const rows = this.uomIssues
			.map((issue) => `
				<tr class="uom-row" data-tally-uom="${esc(issue.tally_uom)}">
					<td style="font-weight:600; vertical-align:middle;">${esc(issue.tally_uom)}</td>
					<td class="text-muted text-center" style="width:28px; vertical-align:middle;">→</td>
					<td>
						<select class="form-control input-sm uom-choice">
							<option value="__create__">Create "${esc(issue.erpnext_uom)}" as a new unit</option>
							${existingOptions}
						</select>
					</td>
				</tr>`)
			.join("");

		const n = this.uomIssues.length;
		$("#uom-issue-list").html(`
			<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
				<span class="text-muted small">${n} unit${n === 1 ? "" : "s"} to resolve</span>
				<button class="btn btn-default btn-xs" id="btn-uom-all-create">Set all to "create as new"</button>
			</div>
			<div style="max-height:340px; overflow-y:auto; border:1px solid #e0e6ed; border-radius:6px;">
				<table class="table table-condensed" style="margin:0;">
					<thead>
						<tr>
							<th style="border-top:0;">Tally unit</th>
							<th style="border-top:0;"></th>
							<th style="border-top:0;">What to do</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`);

		$("#btn-uom-all-create").on("click", () => {
			$("#uom-issue-list .uom-choice").val("__create__");
		});
	}

	// Read every row, batch-create any UOMs the user chose to create (one call),
	// build the {tally_uom: final_uom} override map, then advance to the run step.
	resolveUomsAndContinue() {
		if (!this.uomIssues.length) {
			this.uomOverrides = {};
			this.gotoRun();
			return;
		}

		const overrides = {};
		const toCreate = new Set();
		$("#uom-issue-list .uom-row").each((_, el) => {
			const $row = $(el);
			const tally = $row.data("tally-uom");
			const choice = $row.find(".uom-choice").val();
			if (choice === "__create__") {
				const issue = this.uomIssues.find((i) => i.tally_uom === tally);
				const target = issue ? issue.erpnext_uom : tally;
				overrides[tally] = target;
				toCreate.add(target);
			} else {
				overrides[tally] = choice;
			}
		});

		const finish = () => {
			this.uomOverrides = overrides;
			this.gotoRun();
		};

		if (!toCreate.size) {
			finish();
			return;
		}

		frappe.dom.freeze("Creating units…");
		frappe.call({
			method: "tally_migrator.api.create_uoms",
			args: { uom_names: JSON.stringify([...toCreate]) },
			callback: (r) => {
				frappe.dom.unfreeze();
				const res = r.message || {};
				const failed = res.failed || {};
				if (Object.keys(failed).length) {
					const lines = Object.entries(failed)
						.map(([name, reason]) => `<li><strong>${frappe.utils.escape_html(name)}</strong>: ${frappe.utils.escape_html(reason)}</li>`)
						.join("");
					frappe.msgprint({
						title: "Some units couldn't be created",
						indicator: "red",
						message: `<p>Please map these to an existing unit instead, then try again:</p><ul>${lines}</ul>`,
					});
					return;
				}
				finish();
			},
			error: () => frappe.dom.unfreeze(),
		});
	}

	gotoRun() {
		const erpnext = $("#erpnext-company").val();
		$("#run-subtitle").html(
			`Importing from <strong>${frappe.utils.escape_html(this.fileName || "your file")}</strong> ` +
				`into <strong>${frappe.utils.escape_html(erpnext)}</strong>.`
		);
		this.show("section-run");
	}

	// ── Step 4: run ──────────────────────────────────────────────────────────────

	runMigration() {
		const erpnext = $("#erpnext-company").val();
		const overrides = this.uomOverrides || {};

		$("#btn-run").prop("disabled", true);
		$("#btn-back-3").prop("disabled", true);
		$("#error-section").hide();
		$("#results-section").hide();
		$("#progress-section").show();

		// One stable handler reference, registered once and removed by reference, so
		// repeated runs don't stack duplicate listeners and we never tear down other
		// pages' "progress" subscribers with a blanket off("progress").
		if (!this._onProgress) {
			this._onProgress = (data) => {
				if (data.title !== "Tally Masters Migration") return;
				const pct = data.percent || 0;
				$("#progress-bar").css("width", pct + "%").text(pct + "%");
				$("#progress-desc").text(data.description || "");
			};
		}
		frappe.realtime.off("progress", this._onProgress);
		frappe.realtime.on("progress", this._onProgress);

		frappe.call({
			method: "tally_migrator.api.run_masters_migration_from_file",
			args: {
				file_url: this.fileUrl,
				erpnext_company: erpnext,
				uom_overrides: JSON.stringify(overrides),
				validation_report: this.qualityReport ? JSON.stringify(this.qualityReport) : "",
				record_overrides: JSON.stringify(this.recordOverrides || {}),
				coa_mode: $("#coa-mode").val() || "reuse",
				posting_date: $("#opening-date").val() || "",
			},
			callback: (r) => {
				frappe.realtime.off("progress", this._onProgress);
				$("#progress-bar")
					.removeClass("active progress-bar-striped")
					.css("width", "100%")
					.text("100%");
				const summary = r.message;
				if (summary) {
					this.renderResults(summary);
					$("#run-actions").hide();
				} else {
					$("#btn-run").prop("disabled", false);
					$("#btn-back-3").prop("disabled", false);
				}
			},
			error: (err) => {
				frappe.realtime.off("progress", this._onProgress);
				$("#btn-run").prop("disabled", false);
				$("#btn-back-3").prop("disabled", false);
				const detail =
					(err && (err.message || err._error_message)) ||
					"See the error dialog above for details.";
				$("#error-section")
					.html(
						`<strong>Migration failed.</strong> ${frappe.utils.escape_html(detail)}` +
							`<br><span style="font-size:12px;">Records imported before the failure are kept (each step is committed as it completes), so it's safe to run again — already-imported records are skipped. ` +
							`Open <a href="#" class="err-logs-link">the migration log</a> to see exactly what happened.</span>`
					)
					.show();
				$(".err-logs-link").on("click", (e) => {
					e.preventDefault();
					frappe.set_route("List", "Tally Migration Log");
				});
			},
		});
	}

	renderResults(summary) {
		const logName = summary.log_name;
		// Pull out the per-entity results (everything except our own log_name key)
		const entries = Object.entries(summary).filter(([key]) => key !== "log_name");
		const hasErrors = entries.some(([, r]) => r.failed > 0);
		const totalWarnings = entries.reduce((a, [, r]) => a + (r.warned || 0), 0);
		const totalCreated = entries.reduce((a, [, r]) => a + (r.created || 0), 0);

		// Headline — three states so non-fatal drops (addresses, contacts, opening
		// balances, excluded ledgers) are never hidden behind a green "All done".
		let headlineClass = "alert-success";
		let headlineMsg = `✓ All done! <strong>${totalCreated}</strong> new record${
			totalCreated === 1 ? "" : "s"
		} imported into ERPNext.`;
		if (hasErrors) {
			headlineClass = "alert-warning";
			headlineMsg =
				"⚠ Migration finished — most records imported, but some need your attention (see Failed below).";
		} else if (totalWarnings) {
			headlineClass = "alert-warning";
			headlineMsg = `✓ <strong>${totalCreated}</strong> record${
				totalCreated === 1 ? "" : "s"
			} imported, but <strong>${totalWarnings}</strong> warning${
				totalWarnings === 1 ? "" : "s"
			} need a look — some dependent data (e.g. an address, contact, or opening balance) was dropped. See Warnings below and the migration log.`;
		}
		let html = `<div class="alert ${headlineClass}">${headlineMsg}</div>`;

		// Results table
		html += `
			<table class="table table-bordered table-condensed" style="margin-top:12px;">
				<thead>
					<tr>
						<th>Record type</th>
						<th class="text-right">Imported</th>
						<th class="text-right">Already there</th>
						<th class="text-right">Warnings</th>
						<th class="text-right">Failed</th>
					</tr>
				</thead>
				<tbody>`;
		for (const [label, result] of entries) {
			const warned = result.warned || 0;
			html += `
				<tr>
					<td>${label}</td>
					<td class="text-right text-success"><strong>${result.created}</strong></td>
					<td class="text-right text-muted">${result.skipped}</td>
					<td class="text-right ${warned > 0 ? "text-warning" : "text-muted"}">
						${warned > 0 ? `<strong>${warned}</strong>` : warned}
					</td>
					<td class="text-right ${result.failed > 0 ? "text-danger" : "text-muted"}">
						${result.failed > 0 ? `<strong>${result.failed}</strong>` : result.failed}
					</td>
				</tr>`;
		}
		html += `</tbody></table>`;

		// Plain-English legend
		html += `
			<div class="text-muted small" style="margin-top:6px; line-height:1.6;">
				<strong>Imported</strong> = newly created in ERPNext &nbsp;·&nbsp;
				<strong>Already there</strong> = skipped because it already existed (safe, nothing changed) &nbsp;·&nbsp;
				<strong>Warnings</strong> = imported, but a dependent piece (address, contact, opening balance…) was dropped${totalWarnings ? " — see the log" : ""} &nbsp;·&nbsp;
				<strong>Failed</strong> = couldn't be imported${hasErrors ? " — see the log for the reason" : ""}.
			</div>`;

		// What's next
		const logBtnLabel = logName
			? `View migration log <strong>${frappe.utils.escape_html(logName)}</strong>`
			: "View migration log";
		html += `<div style="margin-top:22px;"><strong>What's next</strong>`;
		html += `<div style="margin-top:10px; display:flex; flex-wrap:wrap; gap:8px;">
				<button class="btn btn-primary btn-sm" id="btn-view-log">${logBtnLabel}</button>
			</div>`;
		html += `<p class="text-muted small" style="margin-top:10px;">
				The migration log lists every record this run touched${hasErrors ? ", including exactly why each failed one didn't import" : ""}${totalWarnings ? ", and each warning where a record imported but a dependent piece was dropped" : ""}.
				${hasErrors || totalWarnings ? "Fix the source in Tally (or in ERPNext), then upload again — records that already imported will simply be skipped." : ""}
			</p>`;
		html += `<div style="margin-top:16px;">
				<button id="btn-restart" class="btn btn-default btn-sm">↺ Migrate another file</button>
			</div></div>`;

		$("#results-section").html(html).show();

		// Wire next-step buttons
		$("#btn-view-log").on("click", () => {
			if (logName) {
				frappe.set_route("Form", "Tally Migration Log", logName);
			} else {
				frappe.set_route("List", "Tally Migration Log");
			}
		});
		$("#btn-restart").on("click", () => this.restart());
	}

	restart() {
		this.fileUrl = null;
		this.fileName = null;
		this.preview = null;
		this.uomIssues = [];
		this.allUoms = [];
		this.uomOverrides = {};
		this.qualityReport = null;
		this.coverageReport = null;
		this.recordOverrides = {};
		this.states = [];
		$("#file-status").html("");
		$("#preview-box").hide().html("");
		$("#btn-next-upload").prop("disabled", true);
		$("#check-loading").show();
		$("#check-clean").hide().removeClass("alert-warning").addClass("alert-success");
		$("#check-issues").hide();
		$("#uom-issue-list").html("");
		$("#coa-mode").val("reuse");
		$("#opening-date").val("");
		$("#dq-section").hide();
		$("#readiness-section").hide().empty();
		$("#coverage-section").hide().empty();
		$("#dq-consent").hide();
		$("#dq-consent-check").prop("checked", false);
		$("#btn-next-check").prop("disabled", false);
		$("#progress-section").hide();
		$("#results-section").hide().html("");
		$("#error-section").hide();
		$("#progress-bar").css("width", "0%").text("0%");
		$("#run-actions").show();
		$("#btn-run").prop("disabled", false);
		$("#btn-back-3").prop("disabled", false);
		this.show("section-upload");
	}
}
