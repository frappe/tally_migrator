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
	// "Review" only appears when the file carries accounts - masters-only files
	// skip it (see visibleSteps / hasAccounts).
	{ id: "section-review", label: "Preview" },
	{ id: "section-run", label: "Migrate" },
];

// Column widths for the inferred-accounts Review table (Tally ledger | Classified
// as | Opening). table-layout:fixed makes the browser honour these instead of
// sizing to content. The shared "why" - no standard Tally ancestor - is stated
// once in the banner above the table, so there's no per-row reason column.
const REVIEW_COLGROUP =
	'<colgroup><col style="width:50%;"><col style="width:30%;">' +
	'<col style="width:20%;"></colgroup>';

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
		this._currentStep = "section-upload";
		this._draftPending = null; // latest unsent draft snapshot (see saveDraft/flushDraft)
		this.render();
		this.loadDraft();          // offer to resume an in-progress migration, if any
		// Flush any pending draft if the user reloads/closes before the debounce fires.
		$(window).on("beforeunload.tallymig", () => this.flushDraft());
	}

	render() {
		$(this.wrapper).find(".page-content").html(`
			${TallyMigratorPage.themeStyle()}
			<div class="container tally-migrator">

				<!-- Persistent stepper -->
				<div id="stepper" class="tm-stepper"></div>

				<!-- STEP 1: Upload -->
				<div id="section-upload">
					<div id="resume-banner" style="display:none;"></div>
					<h4>Bring your Tally data into ERPNext</h4>
					<p class="text-muted tm-lead">
						This tool brings your Tally masters, accounts and opening balances into ERPNext. It
						checks your file and shows you a preview first, so nothing changes until you are ready.
					</p>

					${TallyMigratorPage.callout("info", TallyMigratorPage.iconRow("success", `<strong>Your existing ERPNext data is safe.</strong> Nothing is ever overwritten or deleted. If a record already exists, the migrator skips it. You can run this as many times as you like.`))}

					<div class="tm-section" style="margin-top:var(--margin-lg);">
						<strong>First, export a file from Tally</strong>
						<ol style="margin:var(--margin-sm) 0 0 0; padding-left:20px; font-size:var(--text-md); line-height:1.7;">
							<li>Open your company in Tally Prime.</li>
							<li>Go to <strong>Gateway of Tally → Export → Masters</strong>, then open Configure.</li>
							<li>Set these options:
								<ul style="margin:var(--margin-xs) 0 0 0; padding-left:18px; list-style:disc;">
									<li>Type of master: <strong>All Masters</strong></li>
									<li>Include dependent masters: <strong>Yes</strong></li>
									<li>Export closing balance as opening balance: <strong>Yes</strong> (this becomes your opening balances in ERPNext)</li>
									<li>File Format: <strong>XML (Data Interchange)</strong></li>
								</ul>
							</li>
							<li>Choose Export, and note where the <code>Master.xml</code> file is saved.</li>
						</ol>
					</div>

					<div class="tm-section" style="margin-top:var(--margin-lg);">
						<strong>Then bring it in here</strong>
						<div style="margin-top:var(--margin-sm);">
							<button id="btn-pick-file" class="btn btn-default btn-sm">
								${TallyMigratorPage.navIcon("upload")} &nbsp;Choose Tally XML or .zip file
							</button>
							<span id="file-status" style="margin-left:12px;" class="text-muted"></span>
						</div>
						<p class="text-muted" style="margin:var(--margin-sm) 0 0 0; font-size:var(--text-sm);">
							A large export can be zipped first (XML compresses ~90%) to keep it under the upload
							limit. Too big even zipped? Share it on Google Drive as <strong>Anyone with the
							link</strong>, then in the upload dialog choose <strong>Link</strong> and paste the link.
						</p>
					</div>

					<!-- Preview of what's inside the file -->
					<div id="preview-box" style="display:none; margin-top:var(--margin-lg);"></div>

					<div class="tm-footer" style="justify-content:flex-start;">
						<button id="btn-next-upload" class="btn btn-primary btn-sm" disabled>Continue ${TallyMigratorPage.navIcon("right")}</button>
					</div>
				</div>

				<!-- STEP 2: Configure -->
				<div id="section-configure" style="display:none;">
					<h4>Choose where the data should go</h4>
					<p class="text-muted">Select the ERPNext company that will receive these records.</p>

					<div class="row">
						<div class="form-group col-sm-6">
							<div id="company-control" class="tm-field"></div>
							<div id="company-empty" style="display:none; margin-top:var(--margin-sm);" class="text-muted small">
								No company found. <a href="/app/company/new">Create a Company in ERPNext</a> first,
								then come back and refresh this page.
							</div>
						</div>
					</div>

					<div class="row">
						<div class="form-group col-sm-6">
							<div id="coa-control" class="tm-field"></div>
							<div class="text-muted small tm-field-hint">
								<span id="coa-mode-hint">Tally's reserved groups map onto ERPNext's
								built-in Chart of Accounts; only your custom groups and ledgers are created.</span>
							</div>
						</div>
						<div class="form-group col-sm-6">
							<div id="date-control" class="tm-field"></div>
							<div class="text-muted small tm-field-hint">
								Posting date for opening balances &amp; stock. Leave blank to use the
								company's current fiscal-year start.
							</div>
						</div>
					</div>

					<div class="tm-section" style="margin-top:var(--margin-md);">
						<strong>Here's what will be imported</strong>
						<div id="configure-counts" style="margin-top:var(--margin-sm);"></div>
					</div>

					<div class="tm-footer">
						<button id="btn-back-2" class="btn btn-default btn-sm">${TallyMigratorPage.navIcon("left")} Back</button>
						<button id="btn-next-2" class="btn btn-primary btn-sm">Continue ${TallyMigratorPage.navIcon("right")}</button>
					</div>
				</div>

				<!-- STEP 3: Pre-flight check -->
				<div id="section-check" style="display:none;">
					<h4>Quick check before we begin</h4>
					<p class="text-muted">
						We compare the data in your file against what already exists in ERPNext, so you can
						decide how to handle anything that doesn't match - nothing is changed automatically.
					</p>

					<div id="check-loading" class="text-muted" style="margin:var(--margin-lg) 0;">
						<span class="tm-spin"></span> &nbsp;Checking your file against ERPNext...
					</div>

					<div id="check-clean" class="tm-section" style="display:none;">
						${TallyMigratorPage.callout("success", TallyMigratorPage.iconRow("success", `<strong>Nothing to resolve.</strong> We found no data-quality issues (GST numbers, states, units, HSN codes) that need your input before importing.`))}
					</div>

					<!-- Data-quality report (read-only; informational + consent) -->
					<div id="dq-section" class="tm-section" style="display:none;">
						<div id="dq-cards" class="tm-stats" style="margin-bottom:var(--margin-sm);"></div>
						<div id="dq-list"></div>
					</div>

					<!-- Company-readiness gate (blockers stop the run) -->
					<div id="readiness-section" class="tm-section" style="display:none;"></div>

					<!-- Field-coverage notice (read-only; informational) -->
					<div id="coverage-section" class="tm-section" style="display:none;"></div>

					<div id="check-issues" class="tm-section" style="display:none;">
						<div class="tm-callout tm-callout--info" style="margin-bottom:var(--margin-md);">
							${TallyMigratorPage.iconRow("info", `<strong>Some Units of Measure in your file don't exist in ERPNext yet.</strong> By default we'll create each one as a new unit. Change any row below if you'd rather map it to a unit you already use - then click Continue.`)}
						</div>
						<div id="uom-issue-list"></div>
					</div>

					<!-- Error consent (final gate; shown only when records have errors) -->
					<div id="dq-consent" class="tm-section tm-callout tm-callout--info" style="display:none;">
						<label class="tm-consent">
							<span class="tm-iconrow-icon">
								<input type="checkbox" id="dq-consent-check" style="margin:0;" />
							</span>
							<span>Some records have errors and won't import. Continue with the rest - you can fix and re-import them later from the Migration Log.</span>
						</label>
					</div>


					<div class="tm-footer">
						<div class="tm-footer-group">
							<button id="btn-back-check" class="btn btn-default btn-sm">${TallyMigratorPage.navIcon("left")} Back</button>
							<button id="btn-startover-check" class="btn btn-default btn-sm"
								style="color:var(--text-muted, #8d99a6);">Start over</button>
						</div>
						<button id="btn-next-check" class="btn btn-primary btn-sm">Continue ${TallyMigratorPage.navIcon("right")}</button>
					</div>
				</div>

				<!-- STEP 4: Review accounts (only when the file carries accounts) -->
				<div id="section-review" style="display:none;">
					<h4>Preview your accounts</h4>
					<p class="text-muted">
						Here's how your Tally ledgers will be classified in ERPNext's chart of
						accounts, with their opening balances. Nothing is changed automatically -
						please check anything we've flagged below.
					</p>
					<div id="review-summary" class="tm-section"></div>
					<div id="review-exceptions" class="tm-section"></div>
					<div id="review-all" class="tm-section"></div>
					<div id="review-parties"></div>

					<div class="tm-footer">
						<button id="btn-back-review" class="btn btn-default btn-sm">${TallyMigratorPage.navIcon("left")} Back</button>
						<button id="btn-next-review" class="btn btn-primary btn-sm">Continue ${TallyMigratorPage.navIcon("right")}</button>
					</div>
				</div>

				<!-- STEP 5: Run & Results -->
				<div id="section-run" style="display:none;">
					<h4>Migration</h4>
					<p id="run-subtitle" class="text-muted"></p>

					<div id="run-banner" class="tm-callout tm-section" style="display:none;"></div>

					<div id="progress-section" class="tm-section" style="display:none;">
						<div class="progress" style="margin-bottom:var(--margin-xs);">
							<div id="progress-bar" class="progress-bar progress-bar-striped active" style="width:0%; min-width:2em;">0%</div>
						</div>
						<p id="progress-desc" class="text-muted" style="font-size:var(--text-sm); margin:0;">Starting...</p>
					</div>

					<div id="results-section" style="display:none;"></div>

					<div id="error-section" class="tm-callout tm-callout--error" style="display:none;"></div>

					<div id="stall-section" class="tm-callout tm-callout--info tm-section" style="display:none;"></div>

					<div id="run-actions">
						<div class="tm-footer" style="margin-top:0;">
							<button id="btn-back-3" class="btn btn-default btn-sm">${TallyMigratorPage.navIcon("left")} Back</button>
							<button id="btn-run" class="btn btn-primary btn-sm">Run Migration</button>
						</div>
					</div>
				</div>

			</div>
		`);

		this.renderStepper("section-upload");
		this.mountControls();
		this.bindEvents();
	}

	// ── Step 2 form controls ─────────────────────────────────────────────────────
	// Native Frappe field controls (make_control) replace raw <select>/<input>, so
	// they pick up desk styling, dark mode and keyboard behaviour for free. Value
	// access is centralised through get*/set* helpers below so the rest of the
	// controller never touches the underlying inputs directly.
	mountControls() {
		this.companyControl = frappe.ui.form.make_control({
			parent: $("#company-control")[0],
			df: {
				fieldtype: "Select",
				fieldname: "erpnext_company",
				label: __("ERPNext Company"),
				options: [],
				reqd: 1,
				change: () => this.saveDraft(),
			},
			render_input: true,
		});

		this.coaControl = frappe.ui.form.make_control({
			parent: $("#coa-control")[0],
			df: {
				fieldtype: "Select",
				fieldname: "coa_mode",
				label: __("Chart of Accounts"),
				options: [
					{ value: "reuse", label: __("Reuse ERPNext's standard accounts (recommended)") },
					{ value: "mirror", label: __("Mirror Tally's group tree exactly") },
				],
				default: "reuse",
				change: () => {
					this.updateCoaHint();
					this.saveDraft();
				},
			},
			render_input: true,
		});
		this.coaControl.set_value("reuse");

		this.dateControl = frappe.ui.form.make_control({
			parent: $("#date-control")[0],
			df: {
				fieldtype: "Date",
				fieldname: "posting_date",
				label: __("Opening-balance date"),
				change: () => this.saveDraft(),
			},
			render_input: true,
		});
	}

	// Centralised accessors for the Step 2 controls. Date control stores/returns the
	// system format (yyyy-mm-dd), matching what the API and draft expect.
	getCompany() { return (this.companyControl && this.companyControl.get_value()) || ""; }
	setCompany(v) { if (this.companyControl) this.companyControl.set_value(v || ""); }
	getCoa() { return (this.coaControl && this.coaControl.get_value()) || ""; }
	setCoa(v) { if (this.coaControl) this.coaControl.set_value(v || "reuse"); }
	getDate() { return (this.dateControl && this.dateControl.get_value()) || ""; }
	setDate(v) { if (this.dateControl) this.dateControl.set_value(v || ""); }

	// Populate the company Select with a leading placeholder; an empty list clears it.
	setCompanyOptions(names) {
		if (!this.companyControl) return;
		const opts = names.length
			? [{ value: "", label: __("Select company...") }].concat(
					names.map((n) => ({ value: n, label: n }))
			  )
			: [];
		this.companyControl.df.options = opts;
		this.companyControl.refresh();
		this.companyControl.set_value("");
	}

	updateCoaHint() {
		$("#coa-mode-hint").text(
			this.getCoa() === "mirror"
				? "Every Tally group is recreated verbatim in ERPNext, preserving your exact tree."
				: "Tally's reserved groups map onto ERPNext's built-in Chart of Accounts; only your custom groups and ledgers are created."
		);
	}

	// ── Persistent stepper ──────────────────────────────────────────────────────
	// Frappe default (near-black) for the active step, green for completed,
	// light grey for steps still ahead - matches standard Frappe desk styling.

	// Accounts make the Preview step relevant; masters-only files skip it.
	// Be optimistic: show all 5 steps by default and only drop Preview once we
	// positively confirm the file carries no accounts. Otherwise step 1 (before
	// any file is parsed) would show 4 steps and then grow to 5. Prefer the
	// stable preview signal (known at upload) over accountMapping, which only
	// loads at the Check step.
	hasAccounts() {
		if (this.accountMapping) return this.accountMapping.total_accounts > 0;
		if (this.preview) return this.preview.ledger_accounts > 0;
		return true;
	}

	// The steps actually shown in the stepper - Preview is dropped when the file
	// carries no accounts, so a masters-only run still reads as a clean 4-step flow.
	visibleSteps() {
		return STEPS.filter((s) => s.id !== "section-review" || this.hasAccounts());
	}

	renderStepper(activeId) {
		const steps = this.visibleSteps();
		const activeIdx = steps.findIndex((s) => s.id === activeId);
		const check =
			'<svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true" style="display:block;"><path d="M3.5 8.5l3 3 6-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

		const parts = steps.map((s, i) => {
			const state = i < activeIdx ? "is-done" : i === activeIdx ? "is-active" : "is-pending";
			const dot = i < activeIdx ? check : i + 1;
			const step = `<div class="tm-step ${state}">
					<span class="tm-step-dot">${dot}</span>
					<span class="tm-step-label">${s.label}</span>
				</div>`;
			const connector =
				i < steps.length - 1
					? `<div class="tm-step-line ${i < activeIdx ? "is-done" : ""}"></div>`
					: "";
			return step + connector;
		});
		$("#stepper").html(parts.join(""));
	}

	bindEvents() {
		// Step 1 - upload + advance
		$("#btn-pick-file").on("click", () => this.pickFile());
		$("#btn-next-upload").on("click", () => {
			if (!this.fileUrl) {
				frappe.msgprint(__("Please upload a Tally XML file first."));
				return;
			}
			this.proceedToConfigure();
		});

		// Step 2
		// (COA hint + draft-on-change are wired via each control's df.change in
		// mountControls(), so no jQuery change handlers are needed here.)
		$("#btn-back-2").on("click", () => this.show("section-upload"));
		$("#btn-next-2").on("click", () => {
			const erpnext = this.getCompany();
			if (!erpnext) {
				frappe.msgprint(__("Please select an ERPNext company."));
				return;
			}
			this.proceedToCheck();
		});

		// Step 3 - pre-flight check
		$("#btn-back-check").on("click", () => this.show("section-configure"));
		$("#btn-startover-check").on("click", () => this.confirmStartOver());
		$("#btn-next-check").on("click", () => {
			// Defense in depth: never advance while the gate says blocked (still
			// loading, readiness blockers, or errors without ticked consent) even if a
			// stray render left the button clickable.
			if (this._updateCheckContinue()) return;
			if (this.readiness && this.readiness.ready === false) {
				frappe.msgprint(
					__("This company isn't ready to receive masters. Resolve the blockers shown above, then Re-check.")
				);
				return;
			}
			this.resolveUomsAndContinue();
		});

		// Step 4 - review accounts (skipped when the file has no accounts)
		$("#btn-back-review").on("click", () => this.show("section-check"));
		$("#btn-next-review").on("click", () => this.gotoRun());

		// Step 5 - run. Back lands on Review when it exists, else straight to Check.
		$("#btn-back-3").on("click", () =>
			this.show(this.hasAccounts() ? "section-review" : "section-check"));
		$("#btn-run").on("click", () => this.runMigration());
	}

	// ── Step 1: upload + preview ────────────────────────────────────────────────

	pickFile() {
		new frappe.ui.FileUploader({
			folder: "Home/Attachments",
			restrictions: {
				allowed_file_types: [".xml", "text/xml", "application/xml", ".zip", "application/zip"],
			},
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
			.html(`<span class="text-muted"><span class="tm-spin"></span> &nbsp;Reading your file...</span>`);
		$("#btn-next-upload").prop("disabled", true);

		frappe.call({
			method: "tally_migrator.api.preview_masters_file",
			args: { file_url: this.fileUrl },
			callback: (r) => {
				const p = r.message || {};
				this.preview = p;
				// Now that we know whether the file carries accounts, refresh the
				// stepper so its step count is stable from here on (no 4 -> 5 jump).
				this.renderStepper("section-upload");
				const masters =
					(p.customers || 0) + (p.suppliers || 0) + (p.items || 0) + (p.warehouses || 0);
				const accounts = (p.account_groups || 0) + (p.ledger_accounts || 0);
				if (masters === 0 && accounts === 0) {
					// Nothing at all - the export is empty or not a masters export.
					$("#preview-box").html(
						TallyMigratorPage.callout("error", TallyMigratorPage.iconRow("error", `We read the file, but found no Accounts, Customers, Suppliers, Items or Warehouses in it. Make sure you exported <strong>Masters</strong> (with <strong>Show All Masters = Yes</strong>) from Tally.`))
					);
					$("#btn-next-upload").prop("disabled", true);
					return;
				}
				if (masters === 0) {
					// Accounts but no business masters - a valid chart-of-accounts-only
					// export, but a common sign the user exported a narrower scope than
					// intended. Allow it, but ask them to confirm before spending a run.
					$("#preview-box").html(
						TallyMigratorPage.callout("info", TallyMigratorPage.iconRow("info", `<strong>This looks like a chart-of-accounts-only export.</strong> We found accounts and opening balances, but no Customers, Suppliers, Items or Warehouses. If that is what you intended, continue. If you expected those too, re-export from Tally with <strong>Show All Masters = Yes</strong> and upload again.${this.countsHtml(p)}`))
					);
					$("#btn-next-upload").prop("disabled", false);
					return;
				}
				$("#preview-box").html(
					TallyMigratorPage.callout("success", TallyMigratorPage.iconRow("success", `<strong>File read successfully.</strong> Here's what we found:${this.countsHtml(p)}`))
				);
				$("#btn-next-upload").prop("disabled", false);
			},
			error: () => {
				$("#preview-box").html(
					TallyMigratorPage.callout("error", TallyMigratorPage.iconRow("error", `We couldn't read this file. Please make sure it's a valid Tally <strong>Masters XML</strong> export and upload it again.`))
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
			.map(([label, n]) => `<span class="tm-chip"><strong>${n}</strong> ${label}</span>`)
			.join("");
		return `<div class="tm-chips">${chips}</div>`;
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
		this._currentStep = sectionId;
		this.saveDraft();          // persist progress on every step transition
	}

	// ── Draft persistence (server-side, one per user) ───────────────────────────
	// Survives reload and logout so a half-finished migration - and every inline
	// fix the user made - isn't lost. Debounced; only saves once a file is chosen.

	saveDraft() {
		if (!this.fileUrl) return;
		// Hold the latest snapshot so a reload mid-debounce can still flush it (see
		// flushDraft); cleared once the debounced call actually fires.
		this._draftPending = {
			file_url: this.fileUrl,
			file_name: this.fileName,
			erpnext_company: this.getCompany(),
			coa_mode: this.getCoa(),
			posting_date: this.getDate(),
			step: this._currentStep || "section-upload",
			uom_overrides: this.uomOverrides || {},
			record_overrides: this.recordOverrides || {},
		};
		clearTimeout(this._draftTimer);
		this._draftTimer = setTimeout(() => {
			const payload = this._draftPending;
			if (!payload) return;
			this._draftPending = null;
			frappe.call({
				method: "tally_migrator.api.save_draft",
				args: { payload: JSON.stringify(payload) },
				callback: () => {
					this._draftSaveFailed = false;
				},
				error: () => {
					// Autosave failed (e.g. network drop). Re-queue this payload so the
					// next change - or the unload beacon - retries it, and warn the user
					// once so they know their progress is not being saved. Without this
					// the wizard would silently stop persisting and an accidental reload
					// would lose every fix.
					if (!this._draftPending) this._draftPending = payload;
					if (!this._draftSaveFailed) {
						this._draftSaveFailed = true;
						frappe.show_alert({
							message: __(
								"Couldn't save your progress just now. Your work is still on screen; we'll keep retrying."
							),
							indicator: "orange",
						}, 7);
					}
				},
			});
		}, 600);
	}

	// Best-effort flush of an un-sent draft when the page is being unloaded
	// (reload/close). A normal frappe.call would be cancelled mid-flight, so we use
	// sendBeacon, which the browser delivers after the page is gone. The server
	// reads the CSRF token from the form body, so no custom header is needed.
	flushDraft() {
		const payload = this._draftPending;
		if (!payload || !navigator.sendBeacon) return;
		try {
			const fd = new FormData();
			fd.append("payload", JSON.stringify(payload));
			fd.append("csrf_token", frappe.csrf_token || "");
			navigator.sendBeacon("/api/method/tally_migrator.api.save_draft", fd);
			this._draftPending = null;
		} catch (e) {
			/* best effort - the debounced call or next save will catch up */
		}
	}

	loadDraft() {
		frappe.call({
			method: "tally_migrator.api.get_draft",
			callback: (r) => {
				const d = r.message;
				if (!d || !d.file_url) return;
				const when = d.modified ? frappe.datetime.comment_when(d.modified) : "";
				$("#resume-banner")
					.html(`
						<div class="tm-callout tm-callout--info tm-section" style="display:flex; gap:var(--margin-md); align-items:center; justify-content:space-between;">
							${TallyMigratorPage.iconRow("info", `<div style="font-size:var(--text-md);"><strong>You have an unfinished migration.</strong> File <strong>${frappe.utils.escape_html(d.file_name || d.file_url)}</strong>${when ? ` - last saved ${when}` : ""}. Your fixes are saved.</div>`)}
							<div class="tm-nowrap">
								<button class="btn btn-primary btn-xs" id="btn-resume">Resume</button>
								<button class="btn btn-default btn-xs" id="btn-discard">Start over</button>
							</div>
						</div>`)
					.show();
				$("#btn-resume").on("click", () => this.resumeDraft(d));
				$("#btn-discard").on("click", () => this.confirmStartOver());
			},
			// Intentionally silent on error: a failed draft lookup is non-destructive
			// (the draft, if any, is safe server-side). We simply don't offer a resume
			// banner this load rather than alarming the user on page open; the next load
			// retries. The wizard is fully usable without it.
			error: () => {},
		});
	}

	resumeDraft(d) {
		this.fileUrl = d.file_url;
		this.fileName = d.file_name;
		this.uomOverrides = d.uom_overrides || {};
		this.recordOverrides = d.record_overrides || {};
		// Applied when the Configure step's company list finishes loading.
		this._restore = {
			company: d.erpnext_company,
			coa: d.coa_mode,
			posting: d.posting_date,
			step: d.step,
		};
		$("#resume-banner").hide().empty();
		$("#file-status").html(
			`<span class="indicator green">${frappe.utils.escape_html(this.fileName || this.fileUrl)}</span>`
		);
		// If the user left off on the Migrate step, a job may still be running. Try to
		// reconnect to it directly instead of re-scanning the file first - on a large
		// file that scan is ~minute-long dead time before we'd even discover the run.
		if (d.step === "section-run") {
			this.reconnectOnResume(d);
			return;
		}
		this._resumeNormally(d);
	}

	// Normal resume: re-scan the file, rebuild the wizard, and land the user at the
	// step they left off on (gotoRun auto-attaches to a live run once it gets there).
	_resumeNormally(d) {
		this.loadPreview();        // refresh the counts for the file
		this.proceedToConfigure(); // land on Configure, one click from where they were
		if (this._restore.coa) { this.setCoa(this._restore.coa); this.updateCoaHint(); }
		if (this._restore.posting) this.setDate(this._restore.posting);
		frappe.show_alert({
			message: __("Resumed your in-progress migration - your fixes are saved."),
			indicator: "green",
		});
	}

	// Fast path for resuming on the Migrate step: ask the server if a run is live and,
	// if so, jump straight to tracking it - skipping the heavy file re-scan, which the
	// tracking view doesn't need (results come from the log). If no run is live, fall
	// back to a normal resume.
	reconnectOnResume(d) {
		frappe.call({
			method: "tally_migrator.api.active_run",
			args: { erpnext_company: d.erpnext_company },
			callback: (r) => {
				const live = r && r.message;
				if (!live || !live.log_name) {
					this._resumeNormally(d);
					return;
				}
				// Populate the company picker in the background so the Back button still
				// lands on a usable Configure step. Drop 'step' from _restore so the
				// company-load callback doesn't re-trigger the scan we're skipping.
				this._restore = { company: d.erpnext_company };
				this.loadERPNextCompanies();
				if (d.coa_mode) { this.setCoa(d.coa_mode); this.updateCoaHint(); }
				if (d.posting_date) this.setDate(d.posting_date);
				$("#run-subtitle").html(
					`Importing from <strong>${frappe.utils.escape_html(this.fileName || "your file")}</strong> ` +
						`into <strong>${frappe.utils.escape_html(d.erpnext_company)}</strong>.`
				);
				this.show("section-run");
				this.attachToActiveRun(live);
				frappe.show_alert({
					message: __("Reconnected to your running migration."),
					indicator: "green",
				});
			},
			// On error, don't strand the user on a blank step 5 - fall back to a full resume.
			error: () => this._resumeNormally(d),
		});
	}

	confirmStartOver() {
		// frappe.warn is Frappe's native destructive-confirmation dialog.
		frappe.warn(
			__("Start over?"),
			__("This discards the uploaded file and every fix you've made. This cannot be undone."),
			() => {
				this.clearDraft();
				this.restart();
			},
			__("Discard & start over")
		);
	}

	clearDraft() {
		clearTimeout(this._draftTimer);
		this._draftPending = null;   // don't let the unload beacon re-save a cleared draft
		$("#resume-banner").hide().empty();
		frappe.call({
			method: "tally_migrator.api.clear_draft",
			callback: () => {},
			error: () => {
				// The draft was not deleted server-side, so it would be offered again on
				// the next load. Warn the user rather than letting a stale resume banner
				// reappear unexplained.
				frappe.show_alert({
					message: __(
						"Couldn't discard the saved draft. It may reappear next time; reload and choose Start over again if so."
					),
					indicator: "orange",
				}, 7);
			},
		});
	}

	loadERPNextCompanies() {
		frappe.call({
			method: "tally_migrator.api.get_companies",
			callback: (r) => {
				const companies = r.message || [];
				if (!companies.length) {
					this.setCompanyOptions([]);
					$("#company-empty").show();
					$("#btn-next-2").prop("disabled", true);
					return;
				}
				$("#company-empty").hide();
				$("#btn-next-2").prop("disabled", false);
				this.setCompanyOptions(companies.map((c) => c.name));
				// Restore a resumed company, else auto-select when there is exactly one.
				if (this._restore && this._restore.company) {
					this.setCompany(this._restore.company);
					this.saveDraft();   // persist the restored selection
					// Resume at the step the user actually left off on. Landing on
					// Configure (step 1) every time forced them to re-walk the wizard
					// and re-trigger the scans; jump back to Check/Preview/Migrate with
					// their data (and saved fixes) loaded. The company is needed for the
					// scans, so this can only run once it's restored here.
					const step = this._restore.step;
					this._restore = null;   // consumed - don't re-jump on later Configure visits
					if (step && step !== "section-upload" && step !== "section-configure") {
						this._resumeStep = step;
						this.proceedToCheck();
					}
				} else if (companies.length === 1) {
					this.setCompany(companies[0].name);
				}
			},
			error: () => {
				// A failed load leaves the picker empty, which looks identical to "no
				// companies exist" and silently strands the user on this step. Tell them
				// what actually happened and offer a retry instead.
				this.setCompanyOptions([]);
				$("#btn-next-2").prop("disabled", true);
				$("#company-empty")
					.html(
						__("Couldn't load the company list.") +
							' <a href="#" id="btn-retry-companies">' +
							__("Try again") +
							"</a>"
					)
					.show();
				$("#btn-retry-companies").on("click", (e) => {
					e.preventDefault();
					$("#company-empty").text("").hide();
					this.loadERPNextCompanies();
				});
			},
		});
	}

	// ── Step 3: pre-flight check ─────────────────────────────────────────────────

	proceedToCheck() {
		// Reset only the server-derived scan results - they're recomputed below.
		// The user's own edits (uomOverrides, recordOverrides) must survive: this
		// step is re-entered on every back/forward and right after resuming a draft,
		// so wiping them here silently discarded the user's fixes before the run.
		this.uomIssues = [];
		this.allUoms = [];
		this.qualityReport = null;
		this.coverageReport = null;
		this.accountMapping = null;
		this.readiness = null;
		this.states = [];

		$("#check-loading").show();
		$("#check-clean").hide();
		$("#check-issues").hide();
		$("#dq-section").hide();
		$("#readiness-section").hide();
		$("#coverage-section").hide();
		this.show("section-check");

		// Gate Continue for the whole load window: it stays disabled until both scans
		// return and the gates (readiness / error-consent) permit it - so the user
		// can't click through to Step 4 on data that hasn't loaded yet.
		this._checkLoading = true;
		this._updateCheckContinue();

		// Two independent read-only scans run in parallel: data-quality (GST / HSN /
		// duplicates / collisions) and UOM resolution. Render once both return.
		let pending = 2;
		const done = () => {
			if (--pending > 0) return;
			$("#check-loading").hide();
			this._checkLoading = false;
			this._updateCheckContinue();
			const noUom = !this.uomIssues.length;
			const noDq = !this.qualityReport || this.qualityReport.clean;
			if (noUom && noDq) {
				$("#check-clean").show();
			}
			// When resuming a draft that was past the Check step, advance to the saved
			// step now that the scans (and account mapping) have loaded. Readiness
			// blockers still gate Migrate, exactly as a forward walk would.
			if (this._resumeStep) {
				const step = this._resumeStep;
				this._resumeStep = null;
				if (step === "section-review" && this.hasAccounts()) {
					// Render the preview from the now-loaded account mapping before
					// showing it - mirrors gotoReviewOrRun(); without this the Review
					// section shows blank on resume.
					this.renderAccountMapping();
					this.show("section-review");
				} else if (step === "section-run") {
					this.gotoRun();
				}
			}
		};

		frappe.call({
			method: "tally_migrator.api.validate_masters_data",
			args: {
				file_url: this.fileUrl,
				erpnext_company: this.getCompany(),
				// Apply the user's saved inline fixes (e.g. a resumed draft) so the
				// scan reflects them on first load - otherwise edits stay invisible
				// until the user manually clicks Re-check. Mirrors recheck()'s args.
				record_overrides: JSON.stringify(this.recordOverrides || {}),
				posting_date: this.getDate(),
			},
			callback: (r) => {
				this.qualityReport = r.message || null;
				this.states = (r.message && r.message.states) || [];
				this.coverageReport = (r.message && r.message.coverage) || null;
				this.accountMapping = (r.message && r.message.account_mapping) || null;
				this.readiness = (r.message && r.message.readiness) || null;
				this.renderDataQuality();
				this.renderReadiness();
				this.renderCoverage();
				done();
			},
			error: () => done(),  // non-fatal - importer still reports failures later
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

	// Single owner of the Step-3 Continue button's enabled state. The two async
	// renders (data-quality + readiness) and the consent checkbox all feed into this
	// one function instead of each writing the button directly - so no render can
	// clobber another's gate (the race that let Continue enable without consent), and
	// the button can't be clicked mid-load. Continue is enabled only when: the scans
	// have finished loading, AND the company has no readiness blockers, AND (there are
	// no import errors OR the user ticked the error-consent box). Returns the blocked
	// state so the click handler can double-check before advancing.
	_updateCheckContinue() {
		const loading = !!this._checkLoading;
		const blockers = !!(this.readiness && (this.readiness.blockers || []).length);
		const report = this.qualityReport;
		const hasErrors = !!(report && !report.clean && (report.error_count || 0) > 0);
		const consented = $("#dq-consent-check").is(":checked");
		const blocked = loading || blockers || (hasErrors && !consented);
		$("#btn-next-check").prop("disabled", blocked);
		return blocked;
	}

	// Company-readiness gate. Blockers (a whole entity would fail) disable
	// Continue; warnings (partial degradation) are shown but don't block.
	renderReadiness() {
		const report = this.readiness;
		const $sec = $("#readiness-section");
		if (!report || (report.ready && !(report.warnings || []).length)) {
			$sec.hide().empty();
			this._updateCheckContinue();
			return;
		}
		const esc = frappe.utils.escape_html;
		const icon = TallyMigratorPage.statusIcon;
		const row = (it, kind) => `
			<div style="padding:4px 0; border-top:1px solid var(--border-color, #e0e6ed);">
				<div style="font-weight:600; display:flex; align-items:center; gap:6px;">${icon(kind)} <span>${esc(it.message)}</span></div>
				<div class="text-muted small" style="margin-left:22px;">Fix: ${esc(it.fix)}</div>
			</div>`;
		const blockers = (report.blockers || []).map((b) => row(b, "error")).join("");
		const warnings = (report.warnings || []).map((w) => row(w, "info")).join("");

		const hasBlockers = (report.blockers || []).length > 0;
		const kind = hasBlockers ? "error" : "info";
		const head = hasBlockers
			? TallyMigratorPage.iconRow("error", `<strong>This company isn't ready - fix the items below before migrating.</strong>`)
			: TallyMigratorPage.iconRow("info", `<strong>This company can receive masters, but some steps are degraded.</strong>`);

		$sec.html(TallyMigratorPage.callout(kind, `
				${head}
				${blockers ? `<div style="margin-top:10px;">${blockers}</div>` : ""}
				${warnings ? `<div style="margin-top:10px;">${warnings}</div>` : ""}
				<div style="margin-top:10px;">
					<button class="btn btn-xs btn-default" id="btn-recheck-readiness">${TallyMigratorPage.navIcon("refresh")} Re-check</button>
				</div>
		`)).show();

		$("#btn-recheck-readiness").on("click", () => this.recheckReadiness());
		this._updateCheckContinue();
	}

	// Re-run only the readiness check (after the user fixes setup in another tab),
	// without re-scanning the whole file.
	recheckReadiness() {
		// Disable the button while the call is in flight so a rapid double-click can't
		// queue duplicate readiness checks (the button is re-rendered by renderReadiness).
		const $btn = $("#btn-recheck-readiness");
		if ($btn.prop("disabled")) return;
		$btn.prop("disabled", true);
		frappe.call({
			method: "tally_migrator.api.company_readiness",
			args: { erpnext_company: this.getCompany() },
			callback: (r) => {
				this.readiness = r.message || null;
				this.renderReadiness();
			},
			error: () => $btn.prop("disabled", false),
		});
	}

	// Read-only notice: fields present in the file that we do NOT migrate (Tally
	// UDFs / unmapped attributes). Informational - it never blocks Continue.
	// Friendly names for our OWN object types (a closed, fixed set - safe to label,
	// unlike open-ended Tally tags which are derived, never enumerated).
	static get COVERAGE_ENTITY_NAMES() {
		return {
			Ledger: "Customers, suppliers & accounts",
			"Stock Item": "Items",
			Godown: "Warehouses",
			Group: "Account groups",
			"Cost Centre": "Cost centres",
			"Stock Group": "Item groups",
			Unit: "Units",
		};
	}

	renderCoverage() {
		const report = this.coverageReport;
		const $sec = $("#coverage-section");
		const types = report && report.types ? report.types : [];
		// Only genuine losses (unmapped business data / read-but-not-saved) are worth
		// the user's attention. Step 2's classifier separates real unmapped data from
		// zero-information config constants: a file whose only "unmapped" tags are
		// constants is not a loss, so those are demoted to a muted note (like noise),
		// never the amber alert. Older logs without scores fall back to the raw count.
		const haveScores = !!report && report.meaningful_unmapped_count != null;
		const rawUnmapped = report ? report.unmapped_field_count || 0 : 0;
		const meaningfulUnmapped = haveScores ? report.meaningful_unmapped_count : rawUnmapped;
		const unwritten = report ? report.unwritten_field_count || 0 : 0;
		const lossCount = meaningfulUnmapped + unwritten;    // the REAL loss
		const isMeaningful = (u) => !haveScores || u.score == null || u.score >= 0.2;
		// Speak up ONLY to name a real loss - a business field with no ERPNext home.
		// Everything else (internal noise, config constants, redundant duplicates,
		// recognised-but-skipped taxes like TDS/TCS) is reassurance, not action: it
		// lives on the migration log's full coverage audit, not on this busy screen.
		// For a standard Tally export lossCount is 0, so this section stays silent -
		// the absence of a warning is the reassurance, and it never contradicts the
		// record-level errors/warnings shown above.
		if (!report || lossCount === 0) {
			$sec.hide().empty();
			return;
		}
		const esc = frappe.utils.escape_html;
		const names = TallyMigratorPage.COVERAGE_ENTITY_NAMES;
		const plur = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

		// One plain sentence per lost field: the field's own Tally name (shown
		// verbatim, in the file's original capitalisation), what it contains, where
		// it shows up, and the consequence - no jargon "status" column.
		const line = (u, consequence) => {
			const where = u.count
				? ` (in ${plur(u.count, "record")}${
						u.sample ? `, e.g. "${esc(String(u.sample))}"` : ""
				  })`
				: "";
			const looks = u.kind ? ` Looks like ${esc(u.kind)}.` : "";
			return `<li style="margin-bottom:6px;">
					<strong>${esc(u.field)}</strong>${where}.${looks}
					<span class="text-muted">${consequence}</span>
				</li>`;
		};
		const lossBlocks = types
			.map((t) => {
				const items = [
					...(t.unmapped || []).filter(isMeaningful).map((u) =>
						line(u, "No matching ERPNext field, so it will not be imported.")
					),
					...(t.unwritten || []).map((u) =>
						line(u, "ERPNext has no field to store it, so it will not be imported.")
					),
				].join("");
				if (!items) return "";
				const label = names[t.entity_type] || t.entity_type;
				return `<div style="margin-bottom:10px;">
						<div style="font-weight:600; margin-bottom:4px;">${esc(label)}</div>
						<ul style="margin:0; padding-left:18px;">${items}</ul>
					</div>`;
			})
			.join("");

		// Real loss only reaches here (lossCount > 0): name the fields in plain
		// language so the user can decide whether a custom field matters. This is the
		// one thing the coverage system exists to surface; the reassurance counts
		// (noise / constants / duplicates / taxes) are deliberately NOT shown here -
		// they are on the migration log's full audit.
		$sec.html(
			TallyMigratorPage.callout("info", TallyMigratorPage.iconRow("info", `
						<strong>Most of your data imports fully - but ${plur(
							lossCount,
							"field"
						)} in your file ${
			lossCount === 1 ? "has" : "have"
		} no place in ERPNext.</strong>
						Review what ${
							lossCount === 1 ? "it holds" : "they hold"
						} below: only you can tell whether a custom field matters for your
						business. Nothing is changed automatically, and the full list is
						saved on the migration log.
						<div style="margin-top:var(--margin-sm);">${lossBlocks}</div>
			`))
		).show();
	}

	// ── Dark-mode theming ───────────────────────────────────────────────────────
	// Frappe flips its *semantic* tokens in dark mode (--text-color, --bg-color,
	// --border-color, --card-bg…) but never its *palette* tints (--blue-100,
	// --green-100, --red-100, --gray-100…). Our callouts, pills and stat cards paint
	// those tints as backgrounds while their text rides on --text-color, so in dark
	// mode they'd be near-white text on a pastel box - unreadable. Rather than touch
	// every inline style, we re-point just those tints (and Bootstrap's hardcoded
	// .well) to Frappe's dark surface colours, scoped to this page. Every existing
	// var(--blue-100, …) then resolves correctly in both themes, untouched.
	static themeStyle() {
		return `<style>
			[data-theme="dark"] .tally-migrator {
				--blue-100: #0e2037;   --blue-200: #052b53;
				--green-100: #0b2e1c;  --green-200: #0a3f27;
				--red-100: #361515;    --red-200: #521515;
				--gray-100: #232323;   --gray-200: #2b2b2b;   --gray-300: #343434;
			}

			/* ── Layout ─────────────────────────────────────────────────────────
			   One scoped, token-driven stylesheet replaces ~180 inline styles. All
			   colours/spacing/radii ride on Frappe desk tokens so light/dark mode and
			   the active theme are honoured automatically. */
			.tally-migrator { max-width: 680px; padding-top: var(--padding-lg); padding-bottom: var(--padding-2xl); }
			.tally-migrator h4 { font-size: var(--text-xl); font-weight: 600; margin: 0 0 var(--margin-sm); }
			.tally-migrator h5 { font-size: var(--text-md); font-weight: 600; margin: 0 0 var(--margin-sm); }
			.tally-migrator p { line-height: 1.6; }
			.tally-migrator .tm-lead { margin-bottom: var(--margin-lg); }

			/* ── Stepper ────────────────────────────────────────────────────────── */
			.tally-migrator .tm-stepper { display: flex; align-items: center; margin-bottom: var(--margin-xl); }
			.tally-migrator .tm-step { display: flex; align-items: center; gap: var(--margin-sm); }
			.tally-migrator .tm-step-dot {
				display: inline-flex; align-items: center; justify-content: center;
				width: 24px; height: 24px; border-radius: 50%;
				font-size: var(--text-sm); font-weight: 600;
			}
			.tally-migrator .tm-step-label { font-size: var(--text-md); font-weight: 400; }
			.tally-migrator .tm-step.is-active .tm-step-label { font-weight: 600; color: var(--text-color); }
			.tally-migrator .tm-step.is-done .tm-step-label { color: var(--green-600, #30a66d); }
			.tally-migrator .tm-step.is-pending .tm-step-label { color: var(--text-muted); }
			.tally-migrator .tm-step.is-active .tm-step-dot { background: var(--text-color); color: var(--bg-color); }
			.tally-migrator .tm-step.is-done .tm-step-dot { background: var(--green-600, #30a66d); color: #fff; }
			.tally-migrator .tm-step.is-pending .tm-step-dot { background: var(--gray-300, #d1d8dd); color: #fff; }
			.tally-migrator .tm-step-line { flex: 1; height: 2px; margin: 0 var(--margin-md); background: var(--border-color); }
			.tally-migrator .tm-step-line.is-done { background: var(--green-600, #30a66d); }

			/* ── Footer nav ─────────────────────────────────────────────────────── */
			.tally-migrator .tm-footer {
				margin-top: var(--margin-xl); display: flex;
				justify-content: space-between; align-items: center;
			}
			.tally-migrator .tm-footer-group { display: flex; align-items: center; gap: var(--margin-sm); }

			/* ── Cards & callouts ───────────────────────────────────────────────── */
			.tally-migrator .tm-card {
				border: 1px solid var(--border-color); border-radius: var(--border-radius);
				background: var(--card-bg, #fff);
			}
			.tally-migrator .tm-callout {
				border: 1px solid var(--border-color); border-radius: var(--border-radius);
				padding: var(--padding-md) var(--padding-md); color: var(--text-color);
			}
			.tally-migrator .tm-callout--info { background: var(--blue-100, #edf6fd); border-color: var(--blue-200, #e3f1fd); }
			.tally-migrator .tm-callout--success { background: var(--green-100, #e4f5e9); border-color: var(--green-200, #daf0e1); }
			.tally-migrator .tm-callout--error { background: var(--red-100, #fff0f0); border-color: var(--red-200, #fcd7d7); }
			.tally-migrator .tm-section { margin-bottom: var(--margin-lg); }

			/* ── Icon + text row ────────────────────────────────────────────────── */
			.tally-migrator .tm-iconrow { display: flex; align-items: flex-start; gap: var(--margin-sm); }
			.tally-migrator .tm-iconrow > .tm-iconrow-icon {
				flex: 0 0 auto; display: inline-flex; align-items: center; height: 1.5em;
			}
			.tally-migrator .tm-iconrow > .tm-iconrow-body { flex: 1; min-width: 0; }

			/* ── Stat cards & pills ─────────────────────────────────────────────── */
			.tally-migrator .tm-stats { display: flex; gap: var(--margin-md); }
			.tally-migrator .tm-stat {
				flex: 1; border: 1px solid var(--border-color);
				border-radius: var(--border-radius); padding: var(--padding-sm) var(--padding-md);
			}
			.tally-migrator .tm-stat-num { font-size: var(--text-2xl); font-weight: 700; color: var(--text-color); }
			.tally-migrator .tm-stat-sub { color: var(--text-muted); font-size: var(--text-sm); margin-top: 2px; }
			.tally-migrator .tm-stat-label { margin-top: 5px; }
			.tally-migrator .tm-pill {
				display: inline-block; padding: 1px 12px; border-radius: 10px; font-size: var(--text-sm);
			}
			.tally-migrator .tm-pill--green { background: var(--green-200, #daf0e1); }
			.tally-migrator .tm-pill--blue { background: var(--blue-200, #e3f1fd); }
			.tally-migrator .tm-pill--gray { background: var(--gray-200, #f0f4f7); }
			.tally-migrator .tm-pill--red { background: var(--red-200, #fcd7d7); }

			/* ── Preview count chips ────────────────────────────────────────────── */
			.tally-migrator .tm-chips { margin-top: var(--margin-xs); }
			.tally-migrator .tm-chip {
				display: inline-block; margin: var(--margin-xs) var(--margin-sm) 0 0;
				padding: 3px 10px; background: var(--gray-100, #f4f5f6);
				border-radius: 12px; font-size: var(--text-sm);
			}

			/* ── Tables ─────────────────────────────────────────────────────────── */
			.tally-migrator .tm-table { margin: 0; font-size: var(--text-md); }
			.tally-migrator .tm-table th { border-top: 0; padding: var(--padding-xs) var(--padding-sm); }
			.tally-migrator .tm-table td { padding: var(--padding-xs) var(--padding-sm); }
			.tally-migrator .tm-table .tm-num { text-align: right; white-space: nowrap; }
			.tally-migrator .tm-scroll { max-height: 340px; overflow-y: auto; }
			.tally-migrator .tm-nowrap { white-space: nowrap; }

			/* ── Disclosure (collapsible) header ────────────────────────────────── */
			.tally-migrator .tm-disclosure {
				cursor: pointer; display: flex; align-items: center; justify-content: space-between;
				border: 1px solid var(--border-color); border-radius: var(--border-radius);
				padding: var(--padding-sm) var(--padding-md); color: var(--text-muted);
			}

			/* ── Form fields (Step 2) ───────────────────────────────────────────── */
			.tally-migrator .tm-field { max-width: 360px; margin-bottom: var(--margin-md); }
			.tally-migrator .tm-field-hint { margin-top: var(--margin-xs); }

			/* ── Consent / checkbox row ─────────────────────────────────────────── */
			.tally-migrator .tm-consent { display: flex; align-items: flex-start; gap: var(--margin-sm); margin: 0; font-weight: 400; cursor: pointer; }

			/* ── Inline spinner (native, no FontAwesome dependency) ─────────────── */
			.tally-migrator .tm-spin {
				display: inline-block; width: 12px; height: 12px; vertical-align: -2px;
				border: 2px solid var(--gray-300, #d1d8dd); border-top-color: var(--text-muted, #8d99a6);
				border-radius: 50%; animation: tm-spin 0.7s linear infinite;
			}
			@keyframes tm-spin { to { transform: rotate(360deg); } }

			/* Reusable info tooltip: an inline (i) revealing secondary explanation
			   on hover/focus, so the screen stays terse. */
			.tally-migrator .tm-tip {
				position: relative; display: inline-flex; align-items: center;
				vertical-align: middle; margin-left: 5px; top: -1px;
				color: var(--text-muted, #999); cursor: help;
			}
			.tally-migrator .tm-tip-icon { display: block; }
			.tally-migrator .tm-tip:hover { color: var(--text-color, #1f272e); }
			.tally-migrator .tm-tip-bubble {
				visibility: hidden; opacity: 0;
				position: absolute; bottom: 145%; left: 50%; transform: translateX(-50%);
				width: 240px; max-width: 70vw;
				background: var(--text-color, #1f272e); color: var(--bg-color, #fff);
				text-align: left; font-size: 12px; line-height: 1.45; font-weight: 400;
				padding: 8px 10px; border-radius: 6px;
				box-shadow: 0 4px 14px rgba(0,0,0,0.18); z-index: 1000;
				transition: opacity 0.12s ease; pointer-events: none; white-space: normal;
			}
			.tally-migrator .tm-tip:hover .tm-tip-bubble,
			.tally-migrator .tm-tip:focus .tm-tip-bubble { visibility: visible; opacity: 1; }
		</style>`;
	}

	// ── Shared status vocabulary ────────────────────────────────────────────────
	// One icon family (filled circles) and one callout style across every step, so
	// "good / heads-up / blocked" always look the same. Text stays black; only the
	// background carries colour. Non-blocking notices are blue (info), never amber.
	static statusIcon(kind) {
		const name =
			{ success: "solid-success", info: "solid-info", error: "solid-error" }[kind] ||
			"solid-info";
		return `<span style="display:inline-flex; align-items:center; vertical-align:middle;">${frappe.utils.icon(
			name,
			"sm"
		)}</span>`;
	}

	// Directional / action icons pulled from Frappe's SVG sprite, so navigation and
	// disclosure affordances never fall back to a text glyph used as an icon. `dir`
	// is a sprite name: left, right, down, refresh.
	static navIcon(dir) {
		return `<span style="display:inline-flex; align-items:center; vertical-align:middle;">${frappe.utils.icon(
			dir,
			"sm"
		)}</span>`;
	}

	// Disclosure caret as an SVG: points right when collapsed, down when open.
	static caretIcon(open) {
		return TallyMigratorPage.navIcon(open ? "down" : "right");
	}

	// Icon + content as a row, with the 16px icon vertically centred on the FIRST
	// line of text (not the top of a multi-line block). This is the single source
	// of icon/text alignment for every notice, so they all line up identically.
	static iconRow(kind, html) {
		return `<div class="tm-iconrow">
			<span class="tm-iconrow-icon">${TallyMigratorPage.statusIcon(kind)}</span>
			<div class="tm-iconrow-body">${html}</div>
		</div>`;
	}

	// Background + border use the palette tints, which this page re-points to dark
	// surface colours in dark mode (see themeStyle); text rides on --text-color, which
	// Frappe flips per theme. That pairing keeps every callout readable in both modes -
	// do NOT swap in --text-on-* (e.g. --green-800), as those tints are not remapped for
	// dark mode here and would render dark text on the dark box.
	static callout(kind, inner, extraStyle = "") {
		const variant = { info: "info", success: "success", error: "error" }[kind] || "info";
		const style = extraStyle ? ` style="${extraStyle}"` : "";
		return `<div class="tm-callout tm-callout--${variant}"${style}>${inner}</div>`;
	}

	// Soft status pill (tinted background, no border/icon) and the summary "stat
	// card" used on the Review step. One definition shared by the accounts and
	// party-openings panels so they stay pixel-identical. Background tints:
	// green = good, blue = worth a look, grey = none.
	static get STAT_BG() {
		return { green: "green", blue: "blue", gray: "gray" };
	}
	// `tone` is a tm-pill colour keyword (green / blue / gray). For back-compat a raw
	// CSS colour value still works via an inline fallback.
	static pill(text, tone) {
		if (["green", "blue", "gray", "red"].includes(tone)) {
			return `<span class="tm-pill tm-pill--${tone}">${text}</span>`;
		}
		return `<span class="tm-pill" style="background:${tone};">${text}</span>`;
	}

	// Human elapsed time: "45s", "3m", "3m 20s".
	static fmtElapsed(secs) {
		secs = Math.max(0, Math.round(secs || 0));
		if (secs < 60) return `${secs}s`;
		const m = Math.floor(secs / 60), s = secs % 60;
		return s ? `${m}m ${s}s` : `${m}m`;
	}

	// The single, truthful status line for a tracked run - pure so it is unit-testable.
	// Every branch reflects a real signal, never a time-only guess:
	//   reconnecting  -> the poll request is failing (transient); still tracking
	//   alive === false (with status still Running) -> the worker genuinely stopped
	//   otherwise -> running (a slow phase that reports infrequently is normal)
	// Returns { text, stopped }: `stopped` tells the caller to surface the "open log"
	// affordance and stop claiming live progress.
	static runStatusMessage({ phase, elapsedS, alive, reconnecting } = {}) {
		const t = TallyMigratorPage.fmtElapsed(elapsedS);
		if (reconnecting) {
			return {
				text: `Reconnecting to the server - the migration is still running in ` +
					`the background (${t} elapsed). This page will update automatically.`,
				stopped: false,
			};
		}
		if (alive === false) {
			return {
				text: `The migration worker has stopped responding (${t} elapsed). It ` +
					`will be marked failed - open the migration log for details. Records ` +
					`imported before it stopped are kept, so it is safe to run again.`,
				stopped: true,
			};
		}
		const p = phase ? `${phase} - ` : "";
		return {
			text: `${p}still running (${t} elapsed). This step can be slow and reports ` +
				`infrequently; it is safe to leave this page - the result appears here ` +
				`when it finishes.`,
			stopped: false,
		};
	}

	// Inline (i) that reveals `text` on hover/focus - for secondary explanation we
	// don't want occupying a line of body copy. Keyboard-reachable (tabindex) and
	// screen-reader labelled. Use ONLY for "what does this mean / why", never for
	// anything the user must act on (errors, lost-field names, buttons).
	static infoTip(text) {
		const safe = frappe.utils.escape_html(text);
		const icon =
			`<svg class="tm-tip-icon" width="10" height="10" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
			`<circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.1"/>` +
			`<line x1="8" y1="7.2" x2="8" y2="11.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>` +
			`<circle cx="8" cy="4.8" r="0.8" fill="currentColor"/></svg>`;
		return `<span class="tm-tip" tabindex="0" role="note" aria-label="${safe}">` +
			`${icon}<span class="tm-tip-bubble">${safe}</span></span>`;
	}
	// `tip` (optional) appends an info (i) beside the label - secondary explanation
	// on hover instead of a paragraph below the cards.
	static statCard(big, label, sub, tone, tip = "") {
		const tipIcon = tip ? TallyMigratorPage.infoTip(tip) : "";
		return `
			<div class="tm-stat">
				<div class="text-muted small">${label}${tipIcon}</div>
				<div class="tm-stat-num" style="margin:2px 0 5px;">${big}</div>
				<div>${TallyMigratorPage.pill(sub, tone)}</div>
			</div>`;
	}

	// Wire a collapsible disclosure (header row -> body) so it toggles on click AND
	// on keyboard (Enter / Space), and keeps aria-expanded + the caret in sync. The
	// header must carry role="button" tabindex="0" for the keyboard path to reach it.
	static bindDisclosure(headId, bodyId, caretId) {
		const toggle = () => {
			const $body = $("#" + bodyId);
			$body.toggle();
			const open = $body.is(":visible");
			$("#" + caretId).html(TallyMigratorPage.caretIcon(open));
			$("#" + headId).attr("aria-expanded", open ? "true" : "false");
		};
		$("#" + headId)
			.on("click", toggle)
			.on("keydown", (e) => {
				if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
					e.preventDefault();
					toggle();
				}
			});
	}

	static get DQ_LABELS() {
		return {
			// Errors are real blockers - keep the plain problem framing.
			GSTIN_INVALID: "Invalid GSTIN",
			ITEM_CODE_COLLISION: "Item code collision",
			// Warnings are non-blocking - calm, outcome-first phrasing so they
			// read as "handled, review optional" not "something is wrong".
			GST_STATE_MISSING: "Imports without a GST state",
			GSTIN_STATE_MISMATCH: "GSTIN and state to verify",
			PIN_STATE_CONFLICT: "PIN and state to verify",
			EMAIL_INVALID: "Imports without the email",
			HSN_MISSING: "Imports without HSN",
			DUPLICATE_PARTY: "Possible duplicate to review",
			DUPLICATE_NAME: "Will merge into one",
			CIRCULAR_PARENT: "Hierarchy loop simplified",
		};
	}

	// Render the grouped data-quality report: stat cards + one expandable row per
	// rule code. Editable rules show inline inputs (pre-filled) so the user can fix
	// flagged fields; "Re-check" re-validates the fixes against the same engine.
	// Edits never touch the source file - they ride along as in-memory overrides.
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
					TallyMigratorPage.callout("success", TallyMigratorPage.iconRow("success", `All flagged data issues are resolved.`))
				);
				$("#dq-consent").hide();
				this._updateCheckContinue();
				$("#dq-section").show();
			} else {
				$("#dq-section").hide();
			}
			return;
		}

		const esc = frappe.utils.escape_html;
		// Number stays in the regular text colour; the label below carries a soft
		// colour as a background pill (green = mapped, red = errors, blue = warnings).
		const card = (n, label, tone) => `
			<div class="tm-stat">
				<div class="tm-stat-num">${n}</div>
				<div class="tm-stat-label">${TallyMigratorPage.pill(label, tone)}</div>
			</div>`;
		// Headline shows the number of distinct issue *types* (matching the rows
		// below); the affected-record count is shown inside each group's row.
		const errGroups = report.error_group_count ?? report.error_count;
		const warnGroups = report.warning_group_count ?? report.warning_count;
		// "Mapped" = total records read from the file (customers + suppliers + items).
		const mapped = Object.values(report.totals || {}).reduce((a, b) => a + (b || 0), 0);
		$("#dq-cards").html(
			card(mapped, "Mapped", "green") +
			card(errGroups, "Errors", "red") +
			card(warnGroups, "Warnings", "blue")
		);

		const rows = report.groups.map((g, idx) => this.dqGroupHtml(g, idx)).join("");
		const hasEditable = report.groups.some((g) => (g.editable_fields || []).length);
		const toolbar = hasEditable
			? `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:var(--margin-sm);">
					<span class="text-muted small">Fix a value below, then re-check - or continue anyway.</span>
					<button class="btn btn-default btn-xs" id="btn-dq-recheck">${TallyMigratorPage.navIcon("refresh")} Re-check</button>
				</div>`
			: "";

		$("#dq-list").html(`
			${toolbar}
			<div class="tm-card tm-scroll" style="padding:var(--padding-xs) var(--padding-md);">
				${rows}
			</div>`);

		const toggleDqGroup = (el) => {
			const idx = $(el).data("idx");
			const $body = $("#dq-body-" + idx);
			$body.toggle();
			const open = $body.is(":visible");
			$("#dq-caret-" + idx).html(TallyMigratorPage.caretIcon(open));
			$(el).attr("aria-expanded", open ? "true" : "false");
		};
		$("#dq-list .dq-head")
			.on("click", (e) => toggleDqGroup(e.currentTarget))
			.on("keydown", (e) => {
				if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
					e.preventDefault();
					toggleDqGroup(e.currentTarget);
				}
			});
		// Capture on input (every keystroke), not just change (blur): an edit the
		// user hasn't tabbed away from must still be in memory when they reload or run.
		$("#dq-list .dq-edit").on("input change", (e) => this.captureEdit(e.currentTarget));
		$("#btn-dq-recheck").on("click", () => this.recheck());

		// Errors require explicit consent before Continue. The checkbox feeds the
		// single gate owner (not the button directly), so a later readiness render
		// can't re-enable Continue behind an unticked box.
		if (report.error_count > 0) {
			$("#dq-consent").show();
			$("#dq-consent-check").prop("checked", false).off("change").on("change", () => {
				this._updateCheckContinue();
			});
		} else {
			$("#dq-consent").hide();
		}
		this._updateCheckContinue();
		$("#dq-section").show();
	}

	// One expandable group: header + fix hint + per-record rows (with inline editors
	// when the rule is fixable).
	dqGroupHtml(g, idx) {
		const esc = frappe.utils.escape_html;
		const isErr = g.severity === "error";
		const glyph = isErr
			? frappe.utils.icon("solid-error", "sm")
			: frappe.utils.icon("solid-info", "sm");
		const label = TallyMigratorPage.DQ_LABELS[g.code] || g.code;
		const editable = g.editable_fields || [];
		const items = g.items.map((it) => this.dqItemHtml(it, editable)).join("");
		// First group sits flush under the container's own border, so skip the
		// divider there; later groups keep it to separate them.
		const divider = idx === 0 ? "" : "border-top:1px solid var(--border-color, #f0f4f7);";
		return `
			<div style="${divider}">
				<div class="dq-head" data-idx="${idx}" role="button" tabindex="0" aria-expanded="false" aria-controls="dq-body-${idx}" style="cursor:pointer; padding:8px 0; display:flex; align-items:center; gap:6px;">
					<span style="display:inline-flex; align-items:center;">${glyph}</span>
					<strong>${esc(label)}</strong>
					<span class="text-muted">(${g.items.length})</span>
					<span class="text-muted" style="margin-left:auto;" id="dq-caret-${idx}">${TallyMigratorPage.caretIcon(false)}</span>
				</div>
				${g.fix_hint ? `<div class="text-muted small" style="margin:-2px 0 6px;">${esc(g.fix_hint)}</div>` : ""}
				<div class="dq-body" id="dq-body-${idx}" style="display:none; margin:0 0 8px 16px;">${items}</div>
			</div>`;
	}

	dqItemHtml(it, editableFields) {
		const esc = frappe.utils.escape_html;
		const name = `<div style="padding:2px 0; color:var(--text-color, #555);">
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
			const opts = ['<option value="">- select -</option>']
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
		this.saveDraft();          // persist each inline fix as it's made
	}

	// Re-validate with the in-memory edits applied - fixes are confirmed by the same
	// engine, so resolved issues drop off and any remaining ones stay visible.
	recheck() {
		frappe.dom.freeze(__("Re-checking..."));
		frappe.call({
			method: "tally_migrator.api.validate_masters_data",
			args: {
				file_url: this.fileUrl,
				record_overrides: JSON.stringify(this.recordOverrides),
				// Pass the company + date so the readiness panel (incl. frozen-period
				// checks) is recomputed alongside the data fixes, not left stale.
				erpnext_company: this.getCompany(),
				posting_date: this.getDate(),
			},
			callback: (r) => {
				frappe.dom.unfreeze();
				this.qualityReport = r.message || null;
				this.states = (r.message && r.message.states) || this.states;
				// The server recomputes coverage + account mapping against the fixed
				// data on every call; refresh them too, or the Review step (and the
				// coverage notice) would keep showing the pre-fix snapshot.
				this.coverageReport = (r.message && r.message.coverage) || null;
				this.accountMapping = (r.message && r.message.account_mapping) || null;
				if (r.message && r.message.readiness) {
					this.readiness = r.message.readiness;
					this.renderReadiness();
				}
				this.renderDataQuality();
				this.renderCoverage();
			},
			error: () => frappe.dom.unfreeze(),
		});
	}

	// Compact, scalable table: one row per missing unit. Every row defaults to
	// "create as new", so 3 or 300 issues both resolve in a single Continue
	// click; per-row dropdowns let the user map specific units to existing ones.
	renderUomIssues() {
		const esc = frappe.utils.escape_html;

		// Restore the user's earlier choice for a row: a saved override that maps to
		// an existing unit selects that unit; anything else (or nothing saved) falls
		// back to "create as new". Lets a resumed draft show the choices the user made.
		const savedChoice = (issue) => {
			const sel = (this.uomOverrides || {})[issue.tally_uom];
			return sel && sel !== issue.erpnext_uom ? sel : "__create__";
		};

		const rows = this.uomIssues
			.map((issue) => {
				const chosen = savedChoice(issue);
				// Bare unit names grouped under one "map to existing" heading, so the
				// prefix is shown once (on the optgroup) not on every option.
				const existingOptions = this.allUoms.length
					? `<optgroup label="Or map to an existing unit">${this.allUoms
							.map((u) => `<option value="${esc(u)}" ${u === chosen ? "selected" : ""}>${esc(u)}</option>`)
							.join("")}</optgroup>`
					: "";
				return `
				<tr class="uom-row" data-tally-uom="${esc(issue.tally_uom)}">
					<td style="font-weight:600; vertical-align:middle;">${esc(issue.tally_uom)}</td>
					<td class="text-center" style="width:28px; vertical-align:middle;">${TallyMigratorPage.navIcon("right")}</td>
					<td>
						<select class="form-control input-sm uom-choice">
							<option value="__create__" ${chosen === "__create__" ? "selected" : ""}>Create new unit: "${esc(issue.erpnext_uom)}"</option>
							${existingOptions}
						</select>
					</td>
				</tr>`;
			})
			.join("");

		const n = this.uomIssues.length;
		$("#uom-issue-list").html(`
			<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:var(--margin-sm);">
				<span class="text-muted small">${n} unit${n === 1 ? "" : "s"} to resolve</span>
				<button class="btn btn-default btn-xs" id="btn-uom-all-create">Set all to "create as new"</button>
			</div>
			<div class="tm-card tm-scroll">
				<table class="table table-condensed tm-table">
					<thead>
						<tr>
							<th>Tally unit</th>
							<th></th>
							<th>What to do</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`);

		// Persist each row's choice into the override map as it's made, so a reload
		// before "Continue" keeps the user's UOM decisions (mirrors the inline fixes).
		const persistRow = ($row) => {
			const tally = $row.data("tally-uom");
			const issue = this.uomIssues.find((i) => i.tally_uom === tally);
			if (!issue) return;
			const choice = $row.find(".uom-choice").val();
			this.uomOverrides = this.uomOverrides || {};
			this.uomOverrides[tally] = choice === "__create__" ? issue.erpnext_uom : choice;
		};

		$("#uom-issue-list .uom-choice").on("change", (e) => {
			persistRow($(e.currentTarget).closest(".uom-row"));
			this.saveDraft();
		});

		$("#btn-uom-all-create").on("click", () => {
			$("#uom-issue-list .uom-choice").val("__create__");
			$("#uom-issue-list .uom-row").each((_, el) => persistRow($(el)));
			this.saveDraft();
		});
	}

	// Read every row, batch-create any UOMs the user chose to create (one call),
	// build the {tally_uom: final_uom} override map, then advance to the run step.
	resolveUomsAndContinue() {
		if (!this.uomIssues.length) {
			this.uomOverrides = {};
			this.gotoReviewOrRun();
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
			this.gotoReviewOrRun();
		};

		if (!toCreate.size) {
			finish();
			return;
		}

		frappe.dom.freeze(__("Creating units..."));
		frappe.call({
			method: "tally_migrator.api.create_uoms",
			args: { uom_names: JSON.stringify([...toCreate]) },
			callback: (r) => {
				frappe.dom.unfreeze();
				const res = r.message || {};
				// Remember the units we actually created here so the run can record
				// them in its revert manifest - they're inserted before the run, so the
				// importer skips them and would otherwise orphan them on revert.
				this.createdUoms = (this.createdUoms || []).concat(res.created || []);
				const failed = res.failed || {};
				if (Object.keys(failed).length) {
					const lines = Object.entries(failed)
						.map(([name, reason]) => `<li><strong>${frappe.utils.escape_html(name)}</strong>: ${frappe.utils.escape_html(reason)}</li>`)
						.join("");
					frappe.msgprint({
						title: __("Some units couldn't be created"),
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

	// Branch after the Check step: accounts-bearing files get the Review step;
	// masters-only files go straight to the run.
	gotoReviewOrRun() {
		if (this.hasAccounts()) {
			this.renderAccountMapping();
			this.show("section-review");
		} else {
			this.gotoRun();
		}
	}

	// ── Step 4: review accounts ──────────────────────────────────────────────────
	// Confidence-first: a short summary + only the rows we had to infer, with the
	// full chart of accounts available on demand. All derived from the resolver -
	// no hand-maintained labels. Read-only; never blocks Continue.
	renderAccountMapping() {
		const m = this.accountMapping;
		if (!m || !m.total_accounts) {
			$("#review-summary, #review-exceptions, #review-parties, #review-all").empty();
			return;
		}
		const esc = frappe.utils.escape_html;
		const fmt = (n) => Number(n || 0).toLocaleString("en-IN");
		const inferred = m.inferred_count || 0;
		const confident = m.total_accounts - inferred;
		const plug = m.opening || {};

		// Opening balance cell: amount + Dr/Cr, muted when zero.
		const ob = (r) =>
			r.amount
				? `<span style="white-space:nowrap;">${fmt(r.amount)} <span class="text-muted">${esc(r.dr_cr)}</span></span>`
				: `<span class="text-muted">0</span>`;
		// "--" when the type is genuinely unresolved (no standard group and no Tally
		// nature flags); otherwise the derived/standard root type (+ account type).
		const classifiedAs = (r) =>
			r.uncertain
				? `<span class="text-muted">--</span>`
				: esc(r.root_type) + (r.account_type ? ` · ${esc(r.account_type)}` : "");

		// ── Summary cards ──────────────────────────────────────────────────────
		// Same card language as Step 3: regular-black number, status carried by a
		// soft background pill (green = good, amber = worth a look, grey = none).
		const { green: GREEN_BG, blue: BLUE_BG, gray: GRAY_BG } = TallyMigratorPage.STAT_BG;
		const card = TallyMigratorPage.statCard;

		const plugShare =
			plug.gross_opening > 0
				? ` (about ${Math.round((plug.temporary_opening_plug / plug.gross_opening) * 100)}% of total opening value)`
				: "";
		const plugTip =
			`${fmt(plug.temporary_opening_plug)} ${plug.plug_dr_cr}${plugShare} is held in Temporary Opening ` +
			`to keep your books balanced. It is the part of your Tally opening that does not balance on its own, ` +
			`plus any income/expense opening balances ERPNext cannot carry. This is normal - clear it as you ` +
			`finish your opening entries.`;
		const plugCard = plug.clean
			? card("Balanced", "Opening balances", "Dr = Cr", GREEN_BG)
			: card(
					`${fmt(plug.temporary_opening_plug)} ${esc(plug.plug_dr_cr)}`,
					"Opening balances",
					"posts to Temporary Opening",
					BLUE_BG,
					plugTip
			  );

		$("#review-summary").html(`
			<div class="tm-stats">
				${card(fmt(confident), "Mapped by standard groups", "high confidence", GREEN_BG)}
				${card(
					fmt(inferred),
					"We had to infer",
					inferred ? "please check" : "none",
					inferred ? BLUE_BG : GRAY_BG,
					inferred
						? "These ledgers sit under a custom Tally group, so we inferred each type from the group's own nature. Open the list below to confirm - any shown as \"--\" we couldn't determine."
						: ""
				)}
				${plugCard}
			</div>
		`);

		// ── Exceptions: only the inferred rows, named and explained ────────────
		if (inferred) {
			// Largest opening first - the rows with a real balance (which actually
			// post to the defaulted account) lead; zero-opening rows fall to the end.
			const rows = [...m.inferred]
				.sort((a, b) => (b.amount || 0) - (a.amount || 0))
				.map(
					(r) => `
					<tr>
						<td><strong>${esc(r.name)}</strong></td>
						<td class="text-muted">${classifiedAs(r)}</td>
						<td class="tm-num">${ob(r)}</td>
					</tr>`
				)
				.join("");
			$("#review-exceptions").html(
				TallyMigratorPage.callout("info", `
					${TallyMigratorPage.iconRow("info", `<strong>${fmt(inferred)} account${inferred === 1 ? "" : "s"} we inferred - please confirm.</strong> These ledgers sit under a custom Tally group, so we inferred each type from the group's own nature (income/expense/asset/liability). A type shown as "--" is one we couldn't determine. Confirm these, or fix the group in Tally and re-upload.`)}
					<div class="tm-card" style="margin-top:var(--margin-sm);">
						<table class="table table-condensed tm-table" style="table-layout:fixed;">
							${REVIEW_COLGROUP}
							<thead>
								<tr>
									<th>Tally ledger</th>
									<th>Classified as</th>
									<th class="tm-num">Opening</th>
								</tr>
							</thead>
							<tbody>${rows}</tbody>
						</table>
					</div>
				`)
			);
		} else {
			$("#review-exceptions").html(
				TallyMigratorPage.callout("success", TallyMigratorPage.iconRow("success", `<strong>All ${fmt(m.total_accounts)} accounts mapped using Tally's standard groups.</strong> Nothing needed guessing. Open the full list below if you'd like to review it.`))
			);
		}

		// ── Full chart of accounts (collapsed) ─────────────────────────────────
		const book = (m.groups || [])
			.map((g) => {
				// Each subtotal stays on its own line with the amount glued to its
				// Dr/Cr suffix (nowrap), so a long mixed-sign group never orphans "Cr".
				const sub = [];
				if (g.subtotal_dr) sub.push(`<span class="tm-nowrap">${fmt(g.subtotal_dr)} Dr</span>`);
				if (g.subtotal_cr) sub.push(`<span class="tm-nowrap">${fmt(g.subtotal_cr)} Cr</span>`);
				const accRows = g.accounts
					.map(
						(r) => `
						<tr>
							<td>${esc(r.name)}${
							r.inferred
								? ` ${TallyMigratorPage.statusIcon("info")}`
								: ""
						}</td>
							<td class="text-muted">${esc(r.account_type || "-")}</td>
							<td class="text-muted">${esc(r.parent || "-")}</td>
							<td class="tm-num">${ob(r)}</td>
						</tr>`
					)
					.join("");
				return `
					<tr style="background:var(--fg-color, #f7fafc);">
						<td colspan="3" style="font-weight:600;">${esc(g.root_type)}</td>
						<td class="tm-num" style="font-weight:600;">${sub.join("<br>")}</td>
					</tr>
					${accRows}`;
			})
			.join("");

		$("#review-all").html(`
			<div id="review-all-head" class="tm-disclosure" role="button" tabindex="0" aria-expanded="false" aria-controls="review-all-body">
				<span class="text-muted">Show all ${fmt(m.total_accounts)} mapped accounts</span>
				<span class="text-muted" id="review-all-caret">${TallyMigratorPage.caretIcon(false)}</span>
			</div>
			<div id="review-all-body" class="tm-card tm-scroll" style="display:none; margin-top:var(--margin-sm);">
				<table class="table table-condensed tm-table">
					<thead>
						<tr>
							<th>Tally ledger</th>
							<th>Account type</th>
							<th>Under group</th>
							<th class="tm-num">Opening</th>
						</tr>
					</thead>
					<tbody>${book}</tbody>
				</table>
			</div>
		`);
		TallyMigratorPage.bindDisclosure("review-all-head", "review-all-body", "review-all-caret");

		this.renderPartyOpenings();
	}

	// Party (customer/supplier) opening balances post invoice-wise: one opening
	// invoice per outstanding bill, a payment entry per advance. Show the user the
	// breakdown - and flag any party whose bills did not reconcile to its ledger
	// opening (posted as an "On Account" plug) - before they commit.
	renderPartyOpenings() {
		const p = (this.accountMapping || {}).party_openings;
		if (!p || !p.parties) {
			$("#review-parties").empty();
			return;
		}
		const esc = frappe.utils.escape_html;
		const fmt = (n) => Number(n || 0).toLocaleString("en-IN");
		// Shared card language with Step 3 / the accounts panel (see statCard).
		const { green: GREEN_BG, blue: BLUE_BG, gray: GRAY_BG } = TallyMigratorPage.STAT_BG;
		const card = TallyMigratorPage.statCard;

		// Three cards: outstanding invoices, advances, and a mismatch/lump card that
		// turns amber only when a party's bills did not tie to its ledger opening.
		const cards = [
			card(fmt(p.invoices), "Outstanding invoices", "one opening invoice each", GREEN_BG),
			card(
				fmt(p.advances),
				"Advance receipts/payments",
				p.advances ? "one payment entry each" : "none",
				p.advances ? GREEN_BG : GRAY_BG
			),
			p.on_account
				? card(fmt(p.on_account), "Bills didn't reconcile", "posts 'On Account'", BLUE_BG)
				: card(
						fmt(p.lump),
						"No bill detail",
						p.lump ? "single opening invoice" : "none",
						GRAY_BG,
						"These parties had no bill-wise breakup in Tally. Each gets one opening invoice for its full balance, instead of a separate invoice per outstanding bill."
				  ),
		].join("");

		// Per-party mismatch detail: only the parties whose bills did not add up to
		// the ledger opening - the rows actually worth checking in Tally.
		let warn = "";
		if (p.on_account && (p.mismatches || []).length) {
			// Largest unreconciled gap first - the mismatches most worth checking.
			const rows = [...p.mismatches]
				.sort((a, b) => (b.amount || 0) - (a.amount || 0))
				.map(
					(m) => `
					<tr>
						<td><strong>${esc(m.name)}</strong></td>
						<td class="text-muted">${esc(m.party_type)}</td>
						<td class="tm-num text-muted">${
							m.opening ? `${fmt(m.opening)} ${esc(m.opening_dr_cr || "")}`.trim() : "-"
						}</td>
						<td class="tm-num">${fmt(m.amount)}</td>
					</tr>`
				)
				.join("");
			warn = `<div style="margin-top:var(--margin-md);">` + TallyMigratorPage.callout("info", `
					${TallyMigratorPage.iconRow("info", `<strong>${fmt(p.on_account)} part${p.on_account === 1 ? "y's" : "ies'"} bills didn't add up to the ledger opening.</strong> The 'On Account' figure is the unreconciled gap between the party's bills and its ledger opening (not the total opening) - it posts as an 'On Account' opening so the party still ties to the trial balance. Review these in Tally; a bill may be missing or mis-dated.`)}
					<div class="tm-card" style="margin-top:var(--margin-sm);">
						<table class="table table-condensed tm-table" style="table-layout:fixed;">
							<colgroup><col style="width:32%;"><col style="width:16%;"><col style="width:24%;"><col style="width:28%;"></colgroup>
							<thead>
								<tr>
									<th>Party</th>
									<th>Type</th>
									<th class="tm-num">Ledger opening</th>
									<th class="tm-num">On Account (gap) ${TallyMigratorPage.infoTip(
										"When a party's bills don't add up to its ledger opening, we post the difference as an 'On Account' opening so the party still ties to the trial balance. A non-zero gap is worth checking in Tally - a bill may be missing or mis-dated."
									)}</th>
								</tr>
							</thead>
							<tbody>${rows}</tbody>
						</table>
					</div>
				`) + `</div>`;
		}

		// Collapsed per-party list - the twin of the COA book, so the user can drill
		// into every party's opening, side and document count without it dominating
		// the screen.
		const partyRows = [...(p.parties_list || [])]
			.sort((a, b) => (b.amount || 0) - (a.amount || 0))
			.map((r) => {
				const amt = r.amount
					? `${fmt(r.amount)} ${esc(r.dr_cr || "")}`.trim()
					: "-";
				const flag = r.on_account
					? ` ${TallyMigratorPage.statusIcon("info")}`
					: "";
				return `
					<tr>
						<td>${esc(r.name)}${flag}</td>
						<td class="text-muted">${esc(r.party_type)}</td>
						<td class="tm-num text-muted">${fmt(r.documents)}</td>
						<td class="tm-num">${amt}</td>
					</tr>`;
			})
			.join("");
		const partyBook = partyRows
			? `
			<div id="review-parties-head" class="tm-disclosure" role="button" tabindex="0" aria-expanded="false" aria-controls="review-parties-body" style="margin-top:var(--margin-md);">
				<span class="text-muted">Show all ${fmt(p.parties)} part${p.parties === 1 ? "y" : "ies"}</span>
				<span class="text-muted" id="review-parties-caret">${TallyMigratorPage.caretIcon(false)}</span>
			</div>
			<div id="review-parties-body" class="tm-card tm-scroll" style="display:none; margin-top:var(--margin-sm);">
				<table class="table table-condensed tm-table">
					<thead>
						<tr>
							<th>Party</th>
							<th>Type</th>
							<th class="tm-num">Docs</th>
							<th class="tm-num">Opening</th>
						</tr>
					</thead>
					<tbody>${partyRows}</tbody>
				</table>
			</div>`
			: "";

		// Foreign-currency parties are posted in their own currency (against a
		// per-currency Debtors/Creditors account at Tally's stated rate), so note them
		// for visibility rather than implying the doc count is short.
		const foreignNote = p.foreign
			? `<div style="margin-top:var(--margin-md);">${TallyMigratorPage.callout(
					"info",
					TallyMigratorPage.iconRow(
						"info",
						`<strong>${fmt(p.foreign)} part${
							p.foreign === 1 ? "y" : "ies"
						} use a foreign currency.</strong> Their opening balances are posted in that currency at the rate recorded in Tally - bill-by-bill when the file carries per-bill amounts, otherwise as a single opening invoice.`
					)
			  )}</div>`
			: "";

		$("#review-parties").html(`
			<h5>Customer &amp; supplier opening balances</h5>
			<p class="text-muted" style="margin-bottom:var(--margin-sm); font-size:var(--text-md);">
				${fmt(p.parties)} part${p.parties === 1 ? "y" : "ies"} with an opening balance -
				posted bill-by-bill (${fmt(p.documents)} opening document${p.documents === 1 ? "" : "s"})
				so you can reconcile future payments invoice-by-invoice.
			</p>
			<div class="tm-stats">${cards}</div>
			${warn}
			${foreignNote}
			${partyBook}
		`);
		TallyMigratorPage.bindDisclosure("review-parties-head", "review-parties-body", "review-parties-caret");
	}

	gotoRun() {
		const erpnext = this.getCompany();
		$("#run-subtitle").html(
			`Importing from <strong>${frappe.utils.escape_html(this.fileName || "your file")}</strong> ` +
				`into <strong>${frappe.utils.escape_html(erpnext)}</strong>.`
		);
		this.show("section-run");
		// If a migration for this company is already running (e.g. the user reloaded
		// or resumed mid-run), reconnect to it and track its progress instead of
		// offering to start a second one - which would collide with the live job.
		this.checkActiveRun(erpnext);
	}

	// Ask the server whether a migration is already live for this company; if so,
	// attach to it (track its progress) rather than showing the Run button.
	checkActiveRun(company) {
		if (!company) return;
		frappe.call({
			method: "tally_migrator.api.active_run",
			args: { erpnext_company: company },
			callback: (r) => {
				const live = r && r.message;
				if (live && live.log_name && this._currentStep === "section-run") {
					this.attachToActiveRun(live);
				}
			},
		});
	}

	// Switch step 5 into "tracking" mode for an already-running migration: no Run
	// button, a calm banner, and the same progress stream + log poll a fresh run uses.
	attachToActiveRun(live) {
		$("#run-banner")
			.html(
				`A migration for this company is already running (started ${frappe.utils.escape_html(live.started || "a moment ago")}) - ` +
					`tracking its progress below. You can leave this page; it also updates in the migration log.`
			)
			.show();
		$("#run-actions").hide();
		$("#error-section").hide();
		$("#stall-section").hide();
		$("#results-section").hide();
		$("#progress-section").show();
		this._setupProgressStream();
		this.pollLog(live.log_name);
	}

	// Register the realtime progress handler + heartbeat for a run we are tracking
	// (a fresh run or a reconnected one). Idempotent - safe to call repeatedly.
	_setupProgressStream() {
		if (!this._onProgress) {
			this._onProgress = (data) => {
				if (data.title !== "Tally Masters Migration") return;
				this._lastProgress = Date.now();
				const pct = data.percent || 0;
				$("#progress-bar").css("width", pct + "%").text(pct + "%");
				$("#progress-desc").text(data.description || "");
			};
		}
		frappe.realtime.off("tally_migration_progress", this._onProgress);
		frappe.realtime.on("tally_migration_progress", this._onProgress);
		this._lastProgress = Date.now();
		this._runStart = Date.now();
		this.stopHeartbeat();
		this._heartbeat = setInterval(() => {
			if (Date.now() - this._lastProgress < 8000) return;
			const secs = (Date.now() - this._runStart) / 1000;
			const m = TallyMigratorPage.runStatusMessage(
				{ phase: this._lastSeenDesc, elapsedS: secs });
			$("#progress-desc").text(m.text);
		}, 5000);
	}

	// ── Step 5: run ──────────────────────────────────────────────────────────────

	runMigration() {
		const erpnext = this.getCompany();
		const overrides = this.uomOverrides || {};

		// Disable immediately so a fast double-click can't fire two starts.
		$("#btn-run").prop("disabled", true);
		$("#btn-back-3").prop("disabled", true);
		$("#error-section").hide();
		$("#stall-section").hide();
		$("#results-section").hide();
		$("#run-banner").hide();
		$("#progress-section").show();
		$("#progress-desc").text("Starting...");

		// Re-check for a live run right before starting (covers two open tabs, a stale
		// page, or a click that races the guard). If one is already going, attach to
		// it instead of starting a second; the server guard remains the backstop.
		frappe.call({
			method: "tally_migrator.api.active_run",
			args: { erpnext_company: erpnext },
			callback: (r) => {
				if (r && r.message && r.message.log_name) {
					this.attachToActiveRun(r.message);
					return;
				}
				this._startMigration(erpnext, overrides);
			},
			error: () => this._startMigration(erpnext, overrides),
		});
	}

	_startMigration(erpnext, overrides) {
		this._setupProgressStream();

		frappe.call({
			method: "tally_migrator.api.run_masters_migration_from_file",
			args: {
				file_url: this.fileUrl,
				erpnext_company: erpnext,
				uom_overrides: JSON.stringify(overrides),
				validation_report: this.qualityReport ? JSON.stringify(this.qualityReport) : "",
				record_overrides: JSON.stringify(this.recordOverrides || {}),
				coa_mode: this.getCoa() || "reuse",
				posting_date: this.getDate(),
				created_uoms: JSON.stringify(this.createdUoms || []),
			},
			callback: (r) => {
				const summary = r.message;
				// Large imports run in the background: the server returns {enqueued,
				// log_name} immediately and we track the log to completion (progress
				// keeps streaming over the realtime bus).
				if (summary && summary.enqueued) {
					$("#progress-desc").text(
						"Large import is running in the background. You can leave this " +
						"page; the result will appear here and in the migration log."
					);
					this.pollLog(summary.log_name);
					return;
				}
				this.stopHeartbeat();
				frappe.realtime.off("tally_migration_progress", this._onProgress);
				$("#progress-bar")
					.removeClass("active progress-bar-striped")
					.css("width", "100%")
					.text("100%");
				if (summary) {
					this.clearDraft();   // migration ran - the draft is now obsolete
					this.renderResults(summary);
					$("#run-actions").hide();
				} else {
					$("#btn-run").prop("disabled", false);
					$("#btn-back-3").prop("disabled", false);
				}
			},
			error: (err) => {
				frappe.realtime.off("tally_migration_progress", this._onProgress);
				this.stopHeartbeat();
				$("#progress-section").hide();   // the run failed; don't leave a stuck 0% bar above the error
				$("#btn-run").prop("disabled", false);
				$("#btn-back-3").prop("disabled", false);
				const detail =
					(err && (err.message || err._error_message)) ||
					"See the error dialog above for details.";
				$("#error-section")
					.html(
						`<strong>Migration failed.</strong> ${frappe.utils.escape_html(detail)}` +
							`<br><span style="font-size:12px;">Records imported before the failure are kept (each step is committed as it completes), so it's safe to run again - already-imported records are skipped. ` +
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

	stopHeartbeat() {
		if (this._heartbeat) {
			clearInterval(this._heartbeat);
			this._heartbeat = null;
		}
	}

	// Track a backgrounded run by polling its log until it leaves 'Running',
	// then render the same results table from the log's stored summary.
	pollLog(logName) {
		// Guard against duplicate poll loops for the same log. checkActiveRun (and the
		// fast-path resume reconnect) can call this more than once if the user re-enters
		// step 5 during a live run; without this we'd spawn parallel poll chains and
		// render the result multiple times. Cleared on every terminal state below so
		// "Keep checking" and a post-failure retry can re-arm.
		if (this._trackingLog === logName) return;
		this._trackingLog = logName;
		// pollLog is now the single owner of the step-5 status message (it knows the
		// real status via run_progress and the real liveness via run_liveness). Stop the
		// time-only heartbeat so the two don't fight over #progress-desc.
		this.stopHeartbeat();
		this._trackStart = Date.now();     // when we started tracking (for elapsed)
		this._lastChange = Date.now();     // last time percent/description actually moved
		this._lastSeenPct = null;
		this._lastSeenDesc = null;
		this._pollFails = 0;               // consecutive failed polls (-> "reconnecting")
		this._lastLiveCheck = 0;           // throttle the liveness cross-check
		const finishFromLog = (doc) => {
			this._trackingLog = null;
			frappe.realtime.off("tally_migration_progress", this._onProgress);
			this.stopHeartbeat();
			$("#stall-section").hide();   // clear any "taking longer" notice now the run is terminal
			$("#progress-bar").removeClass("active progress-bar-striped").css("width", "100%").text("100%");
			if (doc.status === "Failed") {
				$("#progress-section").hide();   // the run failed; don't leave a stuck bar above the error
				// A reconnected run hides #run-actions (see attachToActiveRun); re-show it
				// so the user can actually retry, not just find a re-enabled-but-hidden button.
				$("#run-actions").show();
				$("#btn-run").prop("disabled", false);
				$("#btn-back-3").prop("disabled", false);
				$("#error-section")
					.html(
						"<strong>Migration failed.</strong> Records imported before the failure are kept " +
						"(each step is committed as it completes), so it's safe to run again. " +
						'Open <a href="#" class="err-logs-link">the migration log</a> for details.'
					)
					.show();
				$(".err-logs-link").on("click", (e) => {
					e.preventDefault();
					frappe.set_route("Form", "Tally Migration Log", logName);
				});
				return;
			}
			// The run is complete. finishFromLog can win the race against the server's
			// final 100% "Migration complete." progress event, so set the caption here
			// explicitly - otherwise it stays on the stale 95% "Saving migration log..."
			// text under a full bar.
			$("#progress-desc").text("Migration complete.");
			let summary = {};
			try {
				summary = JSON.parse(doc.import_summary || "{}");
			} catch (e) {
			}
			summary.log_name = logName;
			this.clearDraft();
			this.renderResults(summary);
			$("#run-actions").hide();
		};
		// Stop auto-polling after this long without a terminal status. A worker that
		// is hard-killed (OOM, redeploy) never writes 'Failed', so without a cap the
		// log stays 'Running' and the page would poll forever. The run may still be
		// alive (a very large import), so the terminal state is non-committal and
		// offers to keep checking rather than claiming failure.
		const POLL_CAP_MS = 30 * 60 * 1000;
		// How long percent/description may sit unchanged before we stop guessing and
		// ask the server (run_liveness) whether the job is genuinely alive.
		const STALE_MS = 20 * 1000;
		const start = Date.now();

		// confirmedStopped=true means run_liveness told us the worker is genuinely gone
		// (not just quiet), so the copy states that plainly instead of "may still be
		// running". Exposed on the instance so _reportRunStatus can trigger it.
		const stalled = (confirmedStopped) => {
			this._trackingLog = null;
			frappe.realtime.off("tally_migration_progress", this._onProgress);
			this.stopHeartbeat();
			$("#btn-run").prop("disabled", false);
			$("#btn-back-3").prop("disabled", false);
			// Freeze the bar where it stalled - drop the active animation so it no longer
			// implies live progress, but leave it visible as context for how far it got.
			$("#progress-bar").removeClass("active progress-bar-striped");
			$("#progress-desc").text(confirmedStopped
				? "The migration has stopped before completing."
				: "This is taking longer than expected.");
			// The stall notice and its actions live in their own callout (not inline in
			// the progress text and not the error callout) so the "Keep checking" action
			// reads as a real button in a tidy row rather than mid-sentence.
			$("#stall-section")
				.html(
					'<div style="display:flex; align-items:center; justify-content:space-between; ' +
						'gap:var(--margin-md); flex-wrap:wrap;">' +
						'<span style="flex:1; min-width:240px; line-height:1.5;">' +
							(confirmedStopped
								? "The migration worker stopped before finishing (it will be " +
									"marked failed). Records imported before it stopped are kept, " +
									"so it is safe to run again. Open the migration log for details."
								: "The migration may still be running in the background. " +
									"Keep checking, or open the migration log for its live status.") +
						"</span>" +
						'<span style="display:flex; gap:var(--margin-sm); flex-shrink:0;">' +
							'<button class="btn btn-default btn-sm" id="btn-open-log">Open migration log</button>' +
							'<button class="btn btn-primary btn-sm" id="btn-keep-checking">Keep checking</button>' +
						"</span>" +
					"</div>"
				)
				.show();
			$("#run-actions").show();
			$("#btn-open-log").on("click", () => {
				frappe.set_route("Form", "Tally Migration Log", logName);
			});
			$("#btn-keep-checking").on("click", () => {
				$("#stall-section").hide();
				$("#progress-bar").addClass("active progress-bar-striped");
				this.pollLog(logName);
			});
		};

		// Called when percent/description has been unchanged for STALE_MS while still
		// Running. Throttled ask to run_liveness: if the worker is genuinely gone, switch
		// to the "stopped" state; otherwise show an honest running-but-slow line. Never
		// declares "stopped" on a transient lookup blip - run_liveness biases to alive.
		const reportRunStatus = () => {
			const nowMs = Date.now();
			if (nowMs - (this._lastLiveCheck || 0) < 12000) return;   // throttle
			this._lastLiveCheck = nowMs;
			frappe.call({
				method: "tally_migrator.api.run_liveness",
				args: { log_name: logName },
				callback: (r) => {
					const live = r.message || {};
					// Terminal caught here too - let the next run_progress poll render it.
					if (live.status && live.status !== "Running") return;
					const secs = (Date.now() - this._trackStart) / 1000;
					const m = TallyMigratorPage.runStatusMessage(
						{ phase: this._lastSeenDesc, elapsedS: secs, alive: live.alive !== false });
					if (m.stopped) { stalled(true); return; }
					$("#progress-desc").text(m.text);
				},
				// Can't tell right now - stay quiet (keep the last real phase text) rather
				// than alarming; the next poll/liveness check will resolve it.
				error: () => {},
			});
		};

		const poll = () => {
			if (Date.now() - start > POLL_CAP_MS) { stalled(false); return; }
			frappe.call({
				method: "tally_migrator.api.run_progress",
				args: { log_name: logName },
				callback: (r) => {
					const doc = r.message;
					if (!doc) { setTimeout(poll, 3000); return; }
					this._pollFails = 0;              // a good poll clears any "reconnecting"
					if (doc.status === "Running" || !doc.status) {
						// Drive the bar from the persisted progress so a reconnected page
						// advances even when it missed the realtime events (which is what
						// otherwise left it stuck at 0%). Realtime stays the fast-path.
						const moved = doc.percent !== this._lastSeenPct ||
							doc.description !== this._lastSeenDesc;
						if (typeof doc.percent === "number") {
							this._lastProgress = Date.now();
							this._lastSeenPct = doc.percent;
							$("#progress-bar").css("width", doc.percent + "%").text(doc.percent + "%");
						}
						if (doc.description) {
							this._lastSeenDesc = doc.description;
							$("#progress-desc").text(doc.description);
						}
						if (moved) {
							this._lastChange = Date.now();
							$("#stall-section").hide();   // recovered - drop any stopped/stall notice
						} else if (Date.now() - this._lastChange > STALE_MS) {
							// Updates have gone quiet. Don't guess "still working" from the
							// clock - ASK the server whether the job is genuinely alive, and
							// say the truth (running-but-slow vs actually stopped).
							reportRunStatus();
						}
						setTimeout(poll, 3000);
						return;
					}
					finishFromLog(doc);
				},
				// A failed poll (e.g. a 502 while the DB-heavy tail saturates the web
				// workers) must not silently strand the bar. Show an honest "reconnecting"
				// state, keep retrying, and let a recovered poll render the terminal state.
				error: () => {
					this._pollFails = (this._pollFails || 0) + 1;
					if (this._pollFails >= 2) {
						const secs = (Date.now() - this._trackStart) / 1000;
						const m = TallyMigratorPage.runStatusMessage(
							{ phase: this._lastSeenDesc, elapsedS: secs, reconnecting: true });
						$("#progress-desc").text(m.text);
					}
					setTimeout(poll, this._pollFails >= 3 ? 5000 : 2000);
				},
			});
		};
		// Poll immediately so a reconnected bar paints the real percent within a tick,
		// not after the first 3s interval.
		poll();
	}

	renderResults(summary) {
		const logName = summary.log_name;
		// Pull out the per-entity results (everything except our own log_name key)
		const entries = Object.entries(summary).filter(([key]) => key !== "log_name");
		const hasErrors = entries.some(([, r]) => r.failed > 0);
		const totalWarnings = entries.reduce((a, [, r]) => a + (r.warned || 0), 0);
		const totalCreated = entries.reduce((a, [, r]) => a + (r.created || 0), 0);

		// Headline - three states so non-fatal drops (addresses, contacts, opening
		// balances, excluded ledgers) are never hidden behind a green "All done".
		let headlineKind = "success";
		let headlineMsg = `All done! <strong>${totalCreated}</strong> new record${
			totalCreated === 1 ? "" : "s"
		} imported into ERPNext.`;
		if (hasErrors) {
			headlineKind = "error";
			headlineMsg =
				"Migration finished - most records imported, but some need your attention (see Failed below).";
		} else if (totalWarnings) {
			headlineKind = "info";
			headlineMsg = `<strong>${totalCreated}</strong> record${
				totalCreated === 1 ? "" : "s"
			} imported, but <strong>${totalWarnings}</strong> warning${
				totalWarnings === 1 ? "" : "s"
			} need a look. See Warnings below and the migration log.`;
		}
		let html = TallyMigratorPage.callout(headlineKind, TallyMigratorPage.iconRow(headlineKind, `${headlineMsg}`));

		// Results table
		html += `
			<table class="table table-condensed tm-table" style="margin-top:var(--margin-md);">
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
					<td class="text-right"><strong>${result.created}</strong></td>
					<td class="text-right text-muted">${result.skipped}</td>
					<td class="text-right ${warned > 0 ? "" : "text-muted"}"
						${warned > 0 ? `title="${warned} warning${warned === 1 ? "" : "s"}" aria-label="${warned} warnings"` : ""}>
						${warned > 0 ? `<span style="display:inline-flex; align-items:center; gap:4px; justify-content:flex-end;">${TallyMigratorPage.statusIcon("info")}<strong>${warned}</strong></span>` : warned}
					</td>
					<td class="text-right ${result.failed > 0 ? "" : "text-muted"}"
						${result.failed > 0 ? `title="${result.failed} failed" aria-label="${result.failed} failed"` : ""}>
						${result.failed > 0 ? `<span style="display:inline-flex; align-items:center; gap:4px; justify-content:flex-end;">${TallyMigratorPage.statusIcon("error")}<strong>${result.failed}</strong></span>` : result.failed}
					</td>
				</tr>`;
		}
		html += `</tbody></table>`;

		// Plain-English legend
		html += `
			<div class="text-muted small" style="margin-top:var(--margin-xs); line-height:1.6;">
				<strong>Imported</strong> = newly created in ERPNext &nbsp;·&nbsp;
				<strong>Already there</strong> = skipped because it already existed (safe, nothing changed) &nbsp;·&nbsp;
				<strong>Warnings</strong> = imported, but a dependent piece (address, contact, opening balance...) was dropped${totalWarnings ? " - see the log" : ""} &nbsp;·&nbsp;
				<strong>Failed</strong> = couldn't be imported${hasErrors ? " - see the log for the reason" : ""}.
			</div>`;

		// What's next
		const logBtnLabel = logName
			? `View migration log <strong>${frappe.utils.escape_html(logName)}</strong>`
			: "View migration log";
		html += `<div style="margin-top:var(--margin-xl);"><strong>What's next</strong>`;
		html += `<div style="margin-top:var(--margin-sm); display:flex; flex-wrap:wrap; gap:var(--margin-sm);">
				<button class="btn btn-primary btn-sm" id="btn-view-log">${logBtnLabel}</button>
			</div>`;
		html += `<p class="text-muted small" style="margin-top:var(--margin-sm);">
				The migration log lists every record this run touched${hasErrors ? ", including exactly why each failed one didn't import" : ""}${totalWarnings ? ", and each warning where a record imported but a dependent piece was dropped" : ""}.
				${hasErrors || totalWarnings ? "Fix the source in Tally (or in ERPNext), then upload again - records that already imported will simply be skipped." : ""}
			</p>`;
		html += `<div style="margin-top:var(--margin-md);">
				<button id="btn-restart" class="btn btn-default btn-sm">${TallyMigratorPage.navIcon("refresh")} Migrate another file</button>
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
		this._restore = null;
		$("#resume-banner").hide().empty();
		this.preview = null;
		this.uomIssues = [];
		this.allUoms = [];
		this.uomOverrides = {};
		this.qualityReport = null;
		this.coverageReport = null;
		this.accountMapping = null;
		this.readiness = null;
		this._checkLoading = false;
		this.recordOverrides = {};
		this.states = [];
		$("#review-summary, #review-exceptions, #review-all, #review-parties").empty();
		$("#file-status").html("");
		$("#preview-box").hide().html("");
		$("#btn-next-upload").prop("disabled", true);
		$("#check-loading").show();
		$("#check-clean").hide();
		$("#check-issues").hide();
		$("#uom-issue-list").html("");
		this.setCoa("reuse");
		this.updateCoaHint();
		this.setDate("");
		$("#dq-section").hide();
		$("#readiness-section").hide().empty();
		$("#coverage-section").hide().empty();
		$("#dq-consent").hide();
		$("#dq-consent-check").prop("checked", false);
		this._updateCheckContinue();
		$("#progress-section").hide();
		$("#results-section").hide().html("");
		$("#error-section").hide();
		$("#stall-section").hide();
		$("#progress-bar").css("width", "0%").text("0%");
		$("#run-actions").show();
		$("#btn-run").prop("disabled", false);
		$("#btn-back-3").prop("disabled", false);
		this.show("section-upload");
	}
}
