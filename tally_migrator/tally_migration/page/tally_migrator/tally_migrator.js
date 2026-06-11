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

// Shared 4-column widths for the two collapsed Review tables (accounts + parties)
// so they line up column-for-column with each other and header-to-body within each.
// table-layout:fixed makes the browser honour these instead of sizing to content.
const REVIEW_COLGROUP =
	'<colgroup><col style="width:42%;"><col style="width:23%;">' +
	'<col style="width:20%;"><col style="width:15%;"></colgroup>';

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
			<div class="container" style="max-width:680px; padding-top: 24px; padding-bottom: 48px;">

				<!-- Persistent stepper -->
				<div id="stepper" style="display:flex; align-items:center; margin-bottom:28px;"></div>

				<!-- STEP 1: Upload -->
				<div id="section-upload">
					<div id="resume-banner" style="display:none;"></div>
					<h4>Bring your Tally data into ERPNext</h4>
					<p class="text-muted" style="margin-bottom:18px;">
						This tool copies your master records - Customers, Suppliers, Items and Warehouses -
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
						decide how to handle anything that doesn't match - nothing is changed automatically.
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
					</div>

					<!-- Company-readiness gate (blockers stop the run) -->
					<div id="readiness-section" style="display:none; margin-bottom:18px;"></div>

					<!-- Field-coverage notice (read-only; informational) -->
					<div id="coverage-section" style="display:none; margin-bottom:18px;"></div>

					<div id="check-issues" style="display:none;">
						<div class="alert alert-warning" style="margin-bottom:14px;">
							<strong>⚠ Some Units of Measure in your file don't exist in ERPNext yet.</strong>
							By default we'll create each one as a new unit. Change any row below if you'd
							rather map it to a unit you already use - then click Continue.
						</div>
						<div id="uom-issue-list"></div>
					</div>

					<!-- Error consent (final gate; shown only when records have errors) -->
					<div id="dq-consent" style="display:none; margin-bottom:18px;" class="alert alert-info">
						<label style="margin:0; font-weight:400; cursor:pointer; display:flex; align-items:flex-start; gap:8px;">
							<input type="checkbox" id="dq-consent-check" style="margin:3px 0 0; flex:0 0 auto;" />
							<span>Some records have errors and won't import. Continue with the rest - you can fix and re-import them later from the Migration Log.</span>
						</label>
					</div>


					<div style="margin-top:24px;">
						<button id="btn-back-check" class="btn btn-default btn-sm">← Back</button>
						&nbsp;
						<button id="btn-next-check" class="btn btn-primary btn-sm">Continue →</button>
						<button id="btn-startover-check" class="btn btn-default btn-sm pull-right"
							style="color:var(--red-600, #c0392b);">Start over</button>
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
					<div id="review-summary" style="margin-bottom:16px;"></div>
					<div id="review-exceptions" style="margin-bottom:16px;"></div>
					<div id="review-all" style="margin-bottom:16px;"></div>
					<div id="review-parties"></div>

					<div style="margin-top:24px;">
						<button id="btn-back-review" class="btn btn-default btn-sm">← Back</button>
						&nbsp;
						<button id="btn-next-review" class="btn btn-primary btn-sm">Continue →</button>
					</div>
				</div>

				<!-- STEP 5: Run & Results -->
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
		// Use Frappe design tokens (with hex fallbacks) so the stepper matches the
		// active desk theme - including dark mode - instead of hardcoded colours.
		const ACTIVE = "var(--text-color, #1f272e)";    // desk ink / near-black
		const DONE = "var(--green-500, #28a745)";       // Frappe success green
		const PENDING = "var(--gray-300, #d1d8dd)";     // muted fill

		const parts = steps.map((s, i) => {
			const done = i < activeIdx;
			const active = i === activeIdx;
			const circleColor = done ? DONE : active ? ACTIVE : PENDING;
			const textColor = active ? ACTIVE : done ? DONE : "var(--text-muted, #8d99a6)";
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
				i < steps.length - 1
					? `<div style="flex:1; height:2px; background:${i < activeIdx ? DONE : "var(--border-color, #e0e6ed)"}; margin:0 12px;"></div>`
					: "";
			return circle + connector;
		});
		$("#stepper").html(parts.join(""));
	}

	bindEvents() {
		// Step 1 - upload + advance
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

		// Step 3 - pre-flight check
		$("#btn-back-check").on("click", () => this.show("section-configure"));
		$("#btn-startover-check").on("click", () => this.confirmStartOver());
		$("#btn-next-check").on("click", () => {
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

		// Persist option changes to the draft as the user makes them.
		$("#erpnext-company, #coa-mode, #opening-date").on("change", () => this.saveDraft());
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
				// Now that we know whether the file carries accounts, refresh the
				// stepper so its step count is stable from here on (no 4 -> 5 jump).
				this.renderStepper("section-upload");
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
			erpnext_company: $("#erpnext-company").val() || "",
			coa_mode: $("#coa-mode").val() || "",
			posting_date: $("#opening-date").val() || "",
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
				callback: () => {},
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
						<div class="alert alert-warning" style="display:flex; gap:10px; align-items:center; justify-content:space-between;">
							<div style="font-size:13px;">
								<strong>You have an unfinished migration.</strong>
								File <strong>${frappe.utils.escape_html(d.file_name || d.file_url)}</strong>${when ? ` - last saved ${when}` : ""}.
								Your fixes are saved.
							</div>
							<div style="white-space:nowrap;">
								<button class="btn btn-primary btn-xs" id="btn-resume">Resume</button>
								<button class="btn btn-default btn-xs" id="btn-discard">Start over</button>
							</div>
						</div>`)
					.show();
				$("#btn-resume").on("click", () => this.resumeDraft(d));
				$("#btn-discard").on("click", () => this.confirmStartOver());
			},
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
		this.loadPreview();        // refresh the counts for the file
		this.proceedToConfigure(); // land on Configure, one click from where they were
		if (this._restore.coa) $("#coa-mode").val(this._restore.coa).trigger("change");
		if (this._restore.posting) $("#opening-date").val(this._restore.posting);
		frappe.show_alert({
			message: __("Resumed your in-progress migration - your fixes are saved."),
			indicator: "green",
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
		frappe.call({ method: "tally_migrator.api.clear_draft", callback: () => {} });
	}

	loadERPNextCompanies() {
		frappe.call({
			method: "frappe.client.get_list",
			args: { doctype: "Company", fields: ["name"], limit_page_length: 0 },
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
				// Restore a resumed company, else auto-select when there is exactly one.
				if (this._restore && this._restore.company) {
					$select.val(this._restore.company);
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
					$select.val(companies[0].name);
				}
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
			// When resuming a draft that was past the Check step, advance to the saved
			// step now that the scans (and account mapping) have loaded. Readiness
			// blockers still gate Migrate, exactly as a forward walk would.
			if (this._resumeStep) {
				const step = this._resumeStep;
				this._resumeStep = null;
				if (step === "section-review" && this.hasAccounts()) {
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
				erpnext_company: $("#erpnext-company").val(),
				// Apply the user's saved inline fixes (e.g. a resumed draft) so the
				// scan reflects them on first load - otherwise edits stay invisible
				// until the user manually clicks Re-check. Mirrors recheck()'s args.
				record_overrides: JSON.stringify(this.recordOverrides || {}),
				posting_date: $("#opening-date").val() || "",
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
		const blockers = (report.blockers || []).map((b) => row(b, "var(--red-600, #c0392b)")).join("");
		const warnings = (report.warnings || []).map((w) => row(w, "var(--yellow-600, #b8860b)")).join("");

		const hasBlockers = (report.blockers || []).length > 0;
		const cls = hasBlockers ? "alert-danger" : "alert-warning";
		const head = hasBlockers
			? `<strong>✋ This company isn't ready - fix the items below before migrating.</strong>`
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
		// Only genuine losses (unmapped / read-but-not-saved) are worth the user's
		// attention. Redundant duplicates and hidden noise are reassurance, not loss.
		const lossCount =
			(report ? report.unmapped_field_count || 0 : 0) +
			(report ? report.unwritten_field_count || 0 : 0);
		const redundant = report ? report.redundant_field_count || 0 : 0;
		const noise = report ? report.noise_field_count || 0 : 0;
		// Stay silent on a clean file whose only skips are internal noise (every real
		// export has hundreds) - that count still lives on the migration log. Speak up
		// only for a real loss, or to explain a redundant duplicate the user may miss.
		if (!report || (lossCount === 0 && redundant === 0)) {
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
					...(t.unmapped || []).map((u) =>
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

		const redundantNote = redundant
			? `<div style="margin-top:8px; font-size:12px; color:#777;">
					${plur(redundant, "field")} in your file duplicate data we already
					import from elsewhere (e.g. a flat GST number alongside the full GST
					details) - safely skipped, nothing lost.
				</div>`
			: "";
		const noiseNote = noise
			? `<div style="margin-top:6px; font-size:12px; color:#888;">
					${plur(noise, "Tally internal field")} (config flags, empty containers,
					audit / legacy-tax data) were hidden as they carry no business value.
				</div>`
			: "";

		// Tone follows content. With NO real loss, the calm reassurance is honest.
		// With real loss, we must NOT say "nothing to act on" - name the fields and
		// ask the user to review, since only they know if a custom field matters.
		if (lossCount === 0) {
			$sec.html(`
				<div class="alert alert-info" style="margin:0;">
					<div style="display:flex; align-items:flex-start; gap:8px;">
						<span>ℹ</span>
						<div style="flex:1;">
							<strong>All your records will import fully.</strong>
							No fields with a place in ERPNext were left behind.
							${redundantNote}${noiseNote}
						</div>
					</div>
				</div>
			`).show();
			return;
		}

		// Real loss: amber, expanded by default, fields named in plain language.
		$sec.html(`
			<div class="alert alert-warning" style="margin:0;">
				<div style="display:flex; align-items:flex-start; gap:8px;">
					<span>⚠</span>
					<div style="flex:1;">
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
						<div style="margin-top:10px;">${lossBlocks}</div>
						${redundantNote}${noiseNote}
					</div>
				</div>
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
		// Headline shows the number of distinct issue *types* (matching the rows
		// below); the affected-record count is shown inside each group's row.
		const errGroups = report.error_group_count ?? report.error_count;
		const warnGroups = report.warning_group_count ?? report.warning_count;
		$("#dq-cards").html(
			card(errGroups, "Errors", errGroups ? "var(--red-500, #e24c4c)" : "#8d99a6") +
			card(warnGroups, "Warnings", warnGroups ? "var(--yellow-500, #f0a500)" : "#8d99a6")
		);

		const rows = report.groups.map((g, idx) => this.dqGroupHtml(g, idx)).join("");
		const hasEditable = report.groups.some((g) => (g.editable_fields || []).length);
		const toolbar = hasEditable
			? `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
					<span class="text-muted small">Fix a value below, then re-check - or continue anyway.</span>
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
		// Capture on input (every keystroke), not just change (blur): an edit the
		// user hasn't tabbed away from must still be in memory when they reload or run.
		$("#dq-list .dq-edit").on("input change", (e) => this.captureEdit(e.currentTarget));
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
		const dot = g.severity === "error" ? "var(--red-500, #e24c4c)" : "var(--yellow-500, #f0a500)";
		const label = TallyMigratorPage.DQ_LABELS[g.code] || g.code;
		const editable = g.editable_fields || [];
		const items = g.items.map((it) => this.dqItemHtml(it, editable)).join("");
		// First group sits flush under the container's own border, so skip the
		// divider there; later groups keep it to separate them.
		const divider = idx === 0 ? "" : "border-top:1px solid var(--border-color, #f0f4f7);";
		return `
			<div style="${divider}">
				<div class="dq-head" data-idx="${idx}" style="cursor:pointer; padding:8px 0; display:flex; align-items:center; gap:6px;">
					<span style="color:${dot};" title="${g.severity === "error" ? "Error" : "Warning"}" aria-label="${g.severity === "error" ? "Error" : "Warning"}">${g.severity === "error" ? "✗" : "⚠"}</span>
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
		frappe.dom.freeze(__("Re-checking…"));
		frappe.call({
			method: "tally_migrator.api.validate_masters_data",
			args: {
				file_url: this.fileUrl,
				record_overrides: JSON.stringify(this.recordOverrides),
				// Pass the company + date so the readiness panel (incl. frozen-period
				// checks) is recomputed alongside the data fixes, not left stale.
				erpnext_company: $("#erpnext-company").val() || "",
				posting_date: $("#opening-date").val() || "",
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
					<td class="text-muted text-center" style="width:28px; vertical-align:middle;">→</td>
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
				? `${fmt(r.amount)} <span class="text-muted">${esc(r.dr_cr)}</span>`
				: `<span class="text-muted">0</span>`;
		const classifiedAs = (r) =>
			esc(r.root_type) + (r.account_type ? ` · ${esc(r.account_type)}` : "");

		// ── Summary cards ──────────────────────────────────────────────────────
		const AMBER = "var(--yellow-600, #b8860b)";
		const GREEN = "var(--green-600, #1e7e34)";
		const card = (big, label, sub, color) => `
			<div style="flex:1; border:1px solid #e0e6ed; border-radius:6px; padding:10px 12px;">
				<div class="text-muted small">${label}</div>
				<div style="font-size:20px; font-weight:700;${color ? `color:${color};` : ""}">${big}</div>
				<div class="small" style="color:${color || "var(--text-muted, #8d99a6)"};">${sub}</div>
			</div>`;

		const plugCard = plug.clean
			? card("Balanced", "Opening balances", "✓ Dr = Cr", GREEN)
			: card(
					`${fmt(plug.temporary_opening_plug)} ${esc(plug.plug_dr_cr)}`,
					"Opening balances",
					"⚠ posts to Temporary Opening",
					AMBER
			  );

		$("#review-summary").html(`
			<div style="display:flex; gap:10px;">
				${card(fmt(confident), "Mapped by standard groups", "✓ high confidence", GREEN)}
				${card(
					fmt(inferred),
					"We had to infer",
					inferred ? "⚠ please check" : "none",
					inferred ? AMBER : "var(--text-muted, #8d99a6)"
				)}
				${plugCard}
			</div>
		`);

		// ── Exceptions: only the inferred rows, named and explained ────────────
		if (inferred) {
			const rows = m.inferred
				.map(
					(r) => `
					<tr>
						<td style="padding:6px 10px;"><strong>${esc(r.name)}</strong></td>
						<td style="padding:6px 10px;" class="text-muted">${classifiedAs(r)}</td>
						<td style="padding:6px 10px; text-align:right;">${ob(r)}</td>
						<td style="padding:6px 10px;" class="text-muted">no standard Tally group - defaulted</td>
					</tr>`
				)
				.join("");
			$("#review-exceptions").html(`
				<div class="alert alert-warning" style="margin:0;">
					<strong>⚠ ${fmt(inferred)} account${inferred === 1 ? "" : "s"} we inferred - please confirm.</strong>
					These ledgers sit under a custom Tally group with no standard ancestor, so we
					defaulted their type. Only you know if that's right - it's easy to fix the group in Tally and re-upload.
					<div style="margin-top:10px; border:1px solid rgba(0,0,0,0.08); border-radius:6px; overflow:hidden; background:#fff;">
						<table class="table table-condensed" style="margin:0; font-size:13px; table-layout:fixed;">
							${REVIEW_COLGROUP}
							<thead>
								<tr>
									<th style="border-top:0; padding:6px 10px;">Tally ledger</th>
									<th style="border-top:0; padding:6px 10px;">Classified as</th>
									<th style="border-top:0; padding:6px 10px; text-align:right;">Opening</th>
									<th style="border-top:0; padding:6px 10px;">Why flagged</th>
								</tr>
							</thead>
							<tbody>${rows}</tbody>
						</table>
					</div>
				</div>
			`);
		} else {
			$("#review-exceptions").html(`
				<div class="alert alert-success" style="margin:0;">
					<strong>✓ All ${fmt(m.total_accounts)} accounts mapped using Tally's standard groups.</strong>
					Nothing needed guessing. Open the full list below if you'd like to review it.
				</div>
			`);
		}

		// ── Full chart of accounts (collapsed) ─────────────────────────────────
		const book = (m.groups || [])
			.map((g) => {
				const sub = [];
				if (g.subtotal_dr) sub.push(`${fmt(g.subtotal_dr)} Dr`);
				if (g.subtotal_cr) sub.push(`${fmt(g.subtotal_cr)} Cr`);
				const accRows = g.accounts
					.map(
						(r) => `
						<tr>
							<td style="padding:6px 10px;">${esc(r.name)}${
							r.inferred
								? ` <span title="inferred" style="color:${AMBER};">⚠</span>`
								: ""
						}</td>
							<td style="padding:6px 10px;" class="text-muted">${esc(r.account_type || "-")}</td>
							<td style="padding:6px 10px;" class="text-muted">${esc(r.parent || "-")}</td>
							<td style="padding:6px 10px; text-align:right;">${ob(r)}</td>
						</tr>`
					)
					.join("");
				return `
					<tr style="background:var(--fg-color, #f7fafc);">
						<td colspan="3" style="padding:6px 10px; font-weight:600;">${esc(g.root_type)}</td>
						<td style="padding:6px 10px; text-align:right; font-weight:600;">${sub.join(" · ")}</td>
					</tr>
					${accRows}`;
			})
			.join("");

		$("#review-all").html(`
			<div id="review-all-head" style="cursor:pointer; display:flex; align-items:center; justify-content:space-between;
				border:1px solid #e0e6ed; border-radius:6px; padding:10px 12px;">
				<span class="text-muted">Show all ${fmt(m.total_accounts)} mapped accounts</span>
				<span class="text-muted" id="review-all-caret">▸</span>
			</div>
			<div id="review-all-body" style="display:none; margin-top:8px; max-height:360px; overflow-y:auto;
				border:1px solid #e0e6ed; border-radius:6px;">
				<table class="table table-condensed" style="margin:0; font-size:13px;">
					<thead>
						<tr>
							<th style="border-top:0; padding:6px 10px;">Tally ledger</th>
							<th style="border-top:0; padding:6px 10px;">Account type</th>
							<th style="border-top:0; padding:6px 10px;">Under group</th>
							<th style="border-top:0; padding:6px 10px; text-align:right;">Opening</th>
						</tr>
					</thead>
					<tbody>${book}</tbody>
				</table>
			</div>
		`);
		$("#review-all-head").on("click", () => {
			const $body = $("#review-all-body");
			$body.toggle();
			$("#review-all-caret").text($body.is(":visible") ? "▾" : "▸");
		});

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
		const AMBER = "var(--yellow-600, #b8860b)";
		const GREEN = "var(--green-600, #1e7e34)";
		const card = (big, label, sub, color) => `
			<div style="flex:1; border:1px solid #e0e6ed; border-radius:6px; padding:10px 12px;">
				<div class="text-muted small">${label}</div>
				<div style="font-size:20px; font-weight:700;${color ? `color:${color};` : ""}">${big}</div>
				<div class="small" style="color:${color || "var(--text-muted, #8d99a6)"};">${sub}</div>
			</div>`;

		// Three cards: outstanding invoices, advances, and a mismatch/lump card that
		// turns amber only when a party's bills did not tie to its ledger opening.
		const cards = [
			card(fmt(p.invoices), "Outstanding invoices", "one opening invoice each", GREEN),
			card(
				fmt(p.advances),
				"Advance receipts/payments",
				p.advances ? "one payment entry each" : "none",
				p.advances ? null : "var(--text-muted, #8d99a6)"
			),
			p.on_account
				? card(fmt(p.on_account), "Bills didn't reconcile", "⚠ posts 'On Account'", AMBER)
				: card(fmt(p.lump), "No bill detail", p.lump ? "single opening invoice" : "none",
						p.lump ? null : "var(--text-muted, #8d99a6)"),
		].join("");

		// Per-party mismatch detail: only the parties whose bills did not add up to
		// the ledger opening - the rows actually worth checking in Tally.
		let warn = "";
		if (p.on_account && (p.mismatches || []).length) {
			const rows = p.mismatches
				.map(
					(m) => `
					<tr>
						<td style="padding:6px 10px;"><strong>${esc(m.name)}</strong></td>
						<td style="padding:6px 10px;" class="text-muted">${esc(m.party_type)}</td>
						<td style="padding:6px 10px; text-align:right;" class="text-muted">${
							m.opening ? `${fmt(m.opening)} ${esc(m.opening_dr_cr || "")}`.trim() : "-"
						}</td>
						<td style="padding:6px 10px; text-align:right;">${fmt(m.amount)}</td>
					</tr>`
				)
				.join("");
			warn = `
				<div class="alert alert-warning" style="margin:12px 0 0;">
					<strong>⚠ ${fmt(p.on_account)} part${p.on_account === 1 ? "y's" : "ies'"} bills didn't add up to the ledger opening.</strong>
					The 'On Account' figure is the unreconciled gap between the party's bills and its
					ledger opening (not the total opening) - it posts as an 'On Account' opening so the
					party still ties to the trial balance. Review these in Tally; a bill may be missing or mis-dated.
					<div style="margin-top:10px; border:1px solid rgba(0,0,0,0.08); border-radius:6px; overflow:hidden; background:#fff;">
						<table class="table table-condensed" style="margin:0; font-size:13px; table-layout:fixed;">
							${REVIEW_COLGROUP}
							<thead>
								<tr>
									<th style="border-top:0; padding:6px 10px;">Party</th>
									<th style="border-top:0; padding:6px 10px;">Type</th>
									<th style="border-top:0; padding:6px 10px; text-align:right;">Ledger opening</th>
									<th style="border-top:0; padding:6px 10px; text-align:right;">On Account (gap)</th>
								</tr>
							</thead>
							<tbody>${rows}</tbody>
						</table>
					</div>
				</div>`;
		}

		// Collapsed per-party list - the twin of the COA book, so the user can drill
		// into every party's opening, side and document count without it dominating
		// the screen.
		const partyRows = (p.parties_list || [])
			.map((r) => {
				const amt = r.amount
					? `${fmt(r.amount)} ${esc(r.dr_cr || "")}`.trim()
					: "-";
				const flag = r.on_account
					? ` <span title="posts On Account" style="color:${AMBER};">⚠</span>`
					: "";
				return `
					<tr>
						<td style="padding:6px 10px;">${esc(r.name)}${flag}</td>
						<td style="padding:6px 10px;" class="text-muted">${esc(r.party_type)}</td>
						<td style="padding:6px 10px; text-align:right;" class="text-muted">${fmt(r.documents)}</td>
						<td style="padding:6px 10px; text-align:right;">${amt}</td>
					</tr>`;
			})
			.join("");
		const partyBook = partyRows
			? `
			<div id="review-parties-head" style="cursor:pointer; display:flex; align-items:center; justify-content:space-between;
				border:1px solid #e0e6ed; border-radius:6px; padding:10px 12px; margin-top:12px;">
				<span class="text-muted">Show all ${fmt(p.parties)} part${p.parties === 1 ? "y" : "ies"}</span>
				<span class="text-muted" id="review-parties-caret">▸</span>
			</div>
			<div id="review-parties-body" style="display:none; margin-top:8px; max-height:360px; overflow-y:auto;
				border:1px solid #e0e6ed; border-radius:6px;">
				<table class="table table-condensed" style="margin:0; font-size:13px;">
					<thead>
						<tr>
							<th style="border-top:0; padding:6px 10px;">Party</th>
							<th style="border-top:0; padding:6px 10px;">Type</th>
							<th style="border-top:0; padding:6px 10px; text-align:right;">Docs</th>
							<th style="border-top:0; padding:6px 10px; text-align:right;">Opening</th>
						</tr>
					</thead>
					<tbody>${partyRows}</tbody>
				</table>
			</div>`
			: "";

		$("#review-parties").html(`
			<h5 style="margin-bottom:8px;">Customer &amp; supplier opening balances</h5>
			<p class="text-muted" style="margin-bottom:10px; font-size:13px;">
				${fmt(p.parties)} part${p.parties === 1 ? "y" : "ies"} with an opening balance -
				posted bill-by-bill (${fmt(p.documents)} opening document${p.documents === 1 ? "" : "s"})
				so you can reconcile future payments invoice-by-invoice.
			</p>
			<div style="display:flex; gap:10px;">${cards}</div>
			${warn}
			${partyBook}
		`);
		$("#review-parties-head").on("click", () => {
			const $body = $("#review-parties-body");
			$body.toggle();
			$("#review-parties-caret").text($body.is(":visible") ? "▾" : "▸");
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

	// ── Step 5: run ──────────────────────────────────────────────────────────────

	runMigration() {
		const erpnext = $("#erpnext-company").val();
		const overrides = this.uomOverrides || {};

		$("#btn-run").prop("disabled", true);
		$("#btn-back-3").prop("disabled", true);
		$("#error-section").hide();
		$("#results-section").hide();
		$("#progress-section").show();

		// One stable handler reference, registered once and removed by reference, so
		// repeated runs don't stack duplicate listeners. Listens on our own
		// "tally_migration_progress" event (not Frappe's "progress", which also pops
		// the native dialog) so only the step-5 bar reflects the run.
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

		// Heartbeat: if no progress event arrives for a while (e.g. the realtime
		// socket dropped), the striped bar would look frozen even though the run is
		// still going. Show an "elapsed" reassurance so the user isn't left guessing;
		// the authoritative result still arrives via the call's callback / log poll.
		this._lastProgress = Date.now();
		this._runStart = Date.now();
		this.stopHeartbeat();
		this._heartbeat = setInterval(() => {
			if (Date.now() - this._lastProgress < 8000) return;
			const secs = Math.round((Date.now() - this._runStart) / 1000);
			$("#progress-desc").text(
				`Still working… ${secs}s elapsed. Live updates may have paused; the ` +
				`result will appear here when the migration finishes.`
			);
		}, 5000);

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
		const finishFromLog = (doc) => {
			frappe.realtime.off("tally_migration_progress", this._onProgress);
			this.stopHeartbeat();
			$("#progress-bar").removeClass("active progress-bar-striped").css("width", "100%").text("100%");
			if (doc.status === "Failed") {
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
		const start = Date.now();

		const stalled = () => {
			frappe.realtime.off("tally_migration_progress", this._onProgress);
			this.stopHeartbeat();
			$("#btn-run").prop("disabled", false);
			$("#btn-back-3").prop("disabled", false);
			$("#progress-desc").html(
				"This is taking longer than expected. The migration may still be running - " +
				'open <a href="#" class="err-logs-link">the migration log</a> to check its ' +
				'status. <button class="btn btn-xs btn-default" id="btn-keep-checking">Keep checking</button>'
			);
			$(".err-logs-link").on("click", (e) => {
				e.preventDefault();
				frappe.set_route("Form", "Tally Migration Log", logName);
			});
			$("#btn-keep-checking").on("click", () => this.pollLog(logName));
		};

		const poll = () => {
			if (Date.now() - start > POLL_CAP_MS) { stalled(); return; }
			frappe.call({
				method: "frappe.client.get_value",
				args: { doctype: "Tally Migration Log", filters: { name: logName }, fieldname: ["status", "import_summary"] },
				callback: (r) => {
					const doc = r.message;
					if (!doc) { setTimeout(poll, 3000); return; }
					if (doc.status === "Running" || !doc.status) { setTimeout(poll, 3000); return; }
					finishFromLog(doc);
				},
				error: () => setTimeout(poll, 5000),
			});
		};
		setTimeout(poll, 3000);
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
		let headlineClass = "alert-success";
		let headlineMsg = `✓ All done! <strong>${totalCreated}</strong> new record${
			totalCreated === 1 ? "" : "s"
		} imported into ERPNext.`;
		if (hasErrors) {
			headlineClass = "alert-warning";
			headlineMsg =
				"⚠ Migration finished - most records imported, but some need your attention (see Failed below).";
		} else if (totalWarnings) {
			headlineClass = "alert-warning";
			headlineMsg = `✓ <strong>${totalCreated}</strong> record${
				totalCreated === 1 ? "" : "s"
			} imported, but <strong>${totalWarnings}</strong> warning${
				totalWarnings === 1 ? "" : "s"
			} need a look - some dependent data (e.g. an address, contact, or opening balance) was dropped. See Warnings below and the migration log.`;
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
					<td class="text-right ${warned > 0 ? "text-warning" : "text-muted"}"
						${warned > 0 ? `title="${warned} warning${warned === 1 ? "" : "s"}" aria-label="${warned} warnings"` : ""}>
						${warned > 0 ? `<strong>⚠ ${warned}</strong>` : warned}
					</td>
					<td class="text-right ${result.failed > 0 ? "text-danger" : "text-muted"}"
						${result.failed > 0 ? `title="${result.failed} failed" aria-label="${result.failed} failed"` : ""}>
						${result.failed > 0 ? `<strong>✗ ${result.failed}</strong>` : result.failed}
					</td>
				</tr>`;
		}
		html += `</tbody></table>`;

		// Plain-English legend
		html += `
			<div class="text-muted small" style="margin-top:6px; line-height:1.6;">
				<strong>Imported</strong> = newly created in ERPNext &nbsp;·&nbsp;
				<strong>Already there</strong> = skipped because it already existed (safe, nothing changed) &nbsp;·&nbsp;
				<strong>Warnings</strong> = imported, but a dependent piece (address, contact, opening balance…) was dropped${totalWarnings ? " - see the log" : ""} &nbsp;·&nbsp;
				<strong>Failed</strong> = couldn't be imported${hasErrors ? " - see the log for the reason" : ""}.
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
				${hasErrors || totalWarnings ? "Fix the source in Tally (or in ERPNext), then upload again - records that already imported will simply be skipped." : ""}
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
		this._restore = null;
		$("#resume-banner").hide().empty();
		this.preview = null;
		this.uomIssues = [];
		this.allUoms = [];
		this.uomOverrides = {};
		this.qualityReport = null;
		this.coverageReport = null;
		this.accountMapping = null;
		this.recordOverrides = {};
		this.states = [];
		$("#review-summary, #review-exceptions, #review-all").empty();
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
