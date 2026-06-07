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
		this.uomResolutions = {};  // {tally_uom: {action: "map"|"create", value: "..."}}
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

					<div id="check-issues" style="display:none;">
						<div class="alert alert-warning" style="margin-bottom:14px;">
							<strong>⚠ Some Units of Measure in your file don't exist in ERPNext yet.</strong>
							For each one below, choose what to do — your migration won't run until every
							row is resolved.
						</div>
						<div id="uom-issue-list"></div>
					</div>

					<div style="margin-top:24px;">
						<button id="btn-back-check" class="btn btn-default btn-sm">← Back</button>
						&nbsp;
						<button id="btn-next-check" class="btn btn-primary btn-sm" disabled>Continue →</button>
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
			const erpnext = $("#erpnext-company").val();
			$("#run-subtitle").html(
				`Importing from <strong>${frappe.utils.escape_html(this.fileName || "your file")}</strong> ` +
					`into <strong>${frappe.utils.escape_html(erpnext)}</strong>.`
			);
			this.show("section-run");
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
		const rows = [
			["Customers", p.customers || 0],
			["Suppliers", p.suppliers || 0],
			["Items", p.items || 0],
			["Warehouses", p.warehouses || 0],
		];
		const chips = rows
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
					$select.append(`<option value="${c.name}">${c.name}</option>`);
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
		this.uomResolutions = {};

		$("#check-loading").show();
		$("#check-clean").hide();
		$("#check-issues").hide();
		$("#btn-next-check").prop("disabled", true);
		this.show("section-check");

		frappe.call({
			method: "tally_migrator.api.validate_masters_file",
			args: { file_url: this.fileUrl },
			callback: (r) => {
				const data = r.message || {};
				const issues = (data.issues || []).filter((i) => !i.exists);
				this.uomIssues = issues;
				this.allUoms = data.all_uoms || [];

				$("#check-loading").hide();

				if (!issues.length) {
					$("#check-clean").show();
					$("#btn-next-check").prop("disabled", false);
					return;
				}

				$("#check-issues").show();
				this.renderUomIssues();
			},
			error: () => {
				// Non-fatal: let the user proceed; the importer will still surface
				// failures afterwards. We just lose the chance to pre-resolve them.
				$("#check-loading").hide();
				$("#check-clean")
					.show()
					.removeClass("alert-success")
					.addClass("alert-warning")
					.html("Couldn't run the pre-flight check — you can still continue, " +
						"and any issues will be reported after the migration runs.");
				$("#btn-next-check").prop("disabled", false);
			},
		});
	}

	renderUomIssues() {
		const rows = this.uomIssues.map((issue, idx) => {
			const options = this.allUoms
				.map((u) => `<option value="${frappe.utils.escape_html(u)}">${frappe.utils.escape_html(u)}</option>`)
				.join("");
			return `
				<div class="uom-issue-row" data-tally-uom="${frappe.utils.escape_html(issue.tally_uom)}"
					style="border:1px solid #e0e6ed; border-radius:6px; padding:12px 14px; margin-bottom:10px;">
					<div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
						<div style="font-size:13px;">
							Your file uses the unit <strong>"${frappe.utils.escape_html(issue.tally_uom)}"</strong>,
							which would normally become <strong>"${frappe.utils.escape_html(issue.erpnext_uom)}"</strong>
							in ERPNext — but that doesn't exist yet.
						</div>
						<span class="uom-status text-muted small">Not resolved</span>
					</div>
					<div style="display:flex; align-items:center; gap:10px; margin-top:10px; flex-wrap:wrap;">
						<label style="font-size:12px; margin:0;">
							<input type="radio" name="uom-action-${idx}" value="create" checked>
							&nbsp;Create "<strong>${frappe.utils.escape_html(issue.erpnext_uom)}</strong>" in ERPNext
						</label>
						<label style="font-size:12px; margin:0;">
							<input type="radio" name="uom-action-${idx}" value="map">
							&nbsp;Use an existing ERPNext unit instead:
						</label>
						<select class="form-control input-sm uom-map-select" style="width:auto; display:inline-block;" disabled>
							<option value="">Select unit…</option>
							${options}
						</select>
						<button class="btn btn-default btn-xs btn-resolve-uom">Apply</button>
					</div>
				</div>`;
		});
		$("#uom-issue-list").html(rows.join(""));
		this.bindUomRowEvents();
	}

	bindUomRowEvents() {
		const $list = $("#uom-issue-list");

		// Toggle the dropdown depending on which radio is selected
		$list.find(".uom-issue-row").each((_, rowEl) => {
			const $row = $(rowEl);
			$row.find('input[type="radio"]').on("change", () => {
				const action = $row.find('input[type="radio"]:checked').val();
				$row.find(".uom-map-select").prop("disabled", action !== "map");
			});
		});

		$list.find(".btn-resolve-uom").on("click", (e) => {
			const $row = $(e.currentTarget).closest(".uom-issue-row");
			const tallyUom = $row.data("tally-uom");
			const action = $row.find('input[type="radio"]:checked').val();

			if (action === "map") {
				const mapTo = $row.find(".uom-map-select").val();
				if (!mapTo) {
					frappe.msgprint("Please select an existing ERPNext unit to map to.");
					return;
				}
				this.uomResolutions[tallyUom] = { action: "map", value: mapTo };
				$row.find(".uom-status")
					.removeClass("text-muted")
					.addClass("text-success")
					.html(`✓ Will map to "${frappe.utils.escape_html(mapTo)}"`);
				this.maybeEnableCheckContinue();
			} else {
				const issue = this.uomIssues.find((i) => i.tally_uom === tallyUom);
				const target = issue ? issue.erpnext_uom : tallyUom;
				const $btn = $(e.currentTarget);
				$btn.prop("disabled", true).text("Creating…");

				frappe.call({
					method: "frappe.client.insert",
					args: { doc: { doctype: "UOM", uom_name: target, must_be_whole_number: 0 } },
					callback: () => {
						this.uomResolutions[tallyUom] = { action: "create", value: target };
						$row.find(".uom-status")
							.removeClass("text-muted")
							.addClass("text-success")
							.html(`✓ Created "${frappe.utils.escape_html(target)}"`);
						$btn.text("Applied").addClass("disabled");
						this.maybeEnableCheckContinue();
					},
					error: () => {
						$btn.prop("disabled", false).text("Apply");
						$row.find(".uom-status")
							.removeClass("text-muted text-success")
							.addClass("text-danger")
							.text("Couldn't create — try mapping to an existing unit instead.");
					},
				});
			}
		});
	}

	maybeEnableCheckContinue() {
		const allResolved = this.uomIssues.every((i) => this.uomResolutions[i.tally_uom]);
		$("#btn-next-check").prop("disabled", !allResolved);
	}

	// ── Step 4: run ──────────────────────────────────────────────────────────────

	runMigration() {
		const erpnext = $("#erpnext-company").val();

		// Build the override map to send to the backend: tally_uom → final ERPNext UOM
		const overrides = {};
		Object.entries(this.uomResolutions).forEach(([tallyUom, res]) => {
			overrides[tallyUom] = res.value;
		});

		$("#btn-run").prop("disabled", true);
		$("#btn-back-3").prop("disabled", true);
		$("#error-section").hide();
		$("#results-section").hide();
		$("#progress-section").show();

		frappe.realtime.on("progress", (data) => {
			if (data.title !== "Tally Masters Migration") return;
			const pct = data.percent || 0;
			$("#progress-bar").css("width", pct + "%").text(pct + "%");
			$("#progress-desc").text(data.description || "");
		});

		frappe.call({
			method: "tally_migrator.api.run_masters_migration_from_file",
			args: {
				file_url: this.fileUrl,
				erpnext_company: erpnext,
				uom_overrides: JSON.stringify(overrides),
			},
			callback: (r) => {
				frappe.realtime.off("progress");
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
				frappe.realtime.off("progress");
				$("#btn-run").prop("disabled", false);
				$("#btn-back-3").prop("disabled", false);
				const detail =
					(err && (err.message || err._error_message)) ||
					"See the error dialog above for details.";
				$("#error-section")
					.html(
						`<strong>Migration failed.</strong> ${frappe.utils.escape_html(detail)}` +
							`<br><span style="font-size:12px;">Nothing was left half-done — already-imported records are kept and it's safe to run again. ` +
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
		const totalCreated = entries.reduce((a, [, r]) => a + (r.created || 0), 0);

		// Headline
		let html = `
			<div class="alert ${hasErrors ? "alert-warning" : "alert-success"}">
				${
					hasErrors
						? "⚠ Migration finished — most records imported, but some need your attention (see Failed below)."
						: `✓ All done! <strong>${totalCreated}</strong> new record${totalCreated === 1 ? "" : "s"} imported into ERPNext.`
				}
			</div>`;

		// Results table
		html += `
			<table class="table table-bordered table-condensed" style="margin-top:12px;">
				<thead>
					<tr>
						<th>Record type</th>
						<th class="text-right">Imported</th>
						<th class="text-right">Already there</th>
						<th class="text-right">Failed</th>
					</tr>
				</thead>
				<tbody>`;
		for (const [label, result] of entries) {
			html += `
				<tr>
					<td>${label}</td>
					<td class="text-right text-success"><strong>${result.created}</strong></td>
					<td class="text-right text-muted">${result.skipped}</td>
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
				<strong>Failed</strong> = couldn't be imported${hasErrors ? " — see the log for the reason" : ""}.
			</div>`;

		// What's next
		const logBtnLabel = logName
			? `View migration log <strong>${frappe.utils.escape_html(logName)}</strong>`
			: "View migration log";
		html += `<div style="margin-top:22px;"><strong>What's next</strong>`;
		html += `<div style="margin-top:10px; display:flex; flex-wrap:wrap; gap:8px;">
				<button class="btn btn-default btn-sm" data-go="Customer">View Customers</button>
				<button class="btn btn-default btn-sm" data-go="Supplier">View Suppliers</button>
				<button class="btn btn-default btn-sm" data-go="Item">View Items</button>
				<button class="btn btn-default btn-sm" data-go="Warehouse">View Warehouses</button>
				<button class="btn btn-default btn-sm" id="btn-view-log">${logBtnLabel}</button>
			</div>`;
		if (hasErrors) {
			html += `<p class="text-muted small" style="margin-top:12px;">
				To fix the failed records, open the migration log above to see exactly why each one
				failed, correct it in your Tally export (or in ERPNext), then upload again — records
				that already imported will simply be skipped.</p>`;
		}
		html += `<div style="margin-top:16px;">
				<button id="btn-restart" class="btn btn-default btn-sm">↺ Migrate another file</button>
			</div></div>`;

		$("#results-section").html(html).show();

		// Wire next-step buttons
		$("#results-section [data-go]").on("click", (e) => {
			frappe.set_route("List", $(e.currentTarget).attr("data-go"));
		});
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
		this.uomResolutions = {};
		$("#file-status").html("");
		$("#preview-box").hide().html("");
		$("#btn-next-upload").prop("disabled", true);
		$("#check-loading").show();
		$("#check-clean").hide().removeClass("alert-warning").addClass("alert-success");
		$("#check-issues").hide();
		$("#uom-issue-list").html("");
		$("#btn-next-check").prop("disabled", true);
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
