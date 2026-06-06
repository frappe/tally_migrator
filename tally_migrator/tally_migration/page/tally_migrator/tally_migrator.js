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
	{ id: "section-run", label: "Migrate" },
];

class TallyMigratorPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.fileUrl = null;
		this.fileName = null;
		this.preview = null; // {customers, suppliers, items, warehouses}
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
						from Tally into ERPNext. It takes three short steps.
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
					<button id="btn-next-2" class="btn btn-primary btn-sm">Start Migration →</button>
				</div>

				<!-- STEP 3: Run & Results -->
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

	renderStepper(activeId) {
		const activeIdx = STEPS.findIndex((s) => s.id === activeId);
		const parts = STEPS.map((s, i) => {
			const done = i < activeIdx;
			const active = i === activeIdx;
			const circleColor = active ? "#5e64ff" : done ? "#28a745" : "#d1d8dd";
			const textColor = active ? "#1f272e" : "#8d99a6";
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
					? `<div style="flex:1; height:2px; background:${i < activeIdx ? "#28a745" : "#e0e6ed"}; margin:0 12px;"></div>`
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
			$("#run-subtitle").html(
				`Importing from <strong>${frappe.utils.escape_html(this.fileName || "your file")}</strong> ` +
					`into <strong>${frappe.utils.escape_html(erpnext)}</strong>.`
			);
			this.show("section-run");
		});

		// Step 3
		$("#btn-back-3").on("click", () => this.show("section-configure"));
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

	// ── Step 3: run ──────────────────────────────────────────────────────────────

	runMigration() {
		const erpnext = $("#erpnext-company").val();

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
			args: { file_url: this.fileUrl, erpnext_company: erpnext },
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
		const hasErrors = Object.values(summary).some((r) => r.failed > 0);
		const totalCreated = Object.values(summary).reduce((a, r) => a + (r.created || 0), 0);

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
		for (const [label, result] of Object.entries(summary)) {
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
		html += `<div style="margin-top:22px;"><strong>What's next</strong>`;
		html += `<div style="margin-top:10px; display:flex; flex-wrap:wrap; gap:8px;">
				<button class="btn btn-default btn-sm" data-go="Customer">View Customers</button>
				<button class="btn btn-default btn-sm" data-go="Supplier">View Suppliers</button>
				<button class="btn btn-default btn-sm" data-go="Item">View Items</button>
				<button class="btn btn-default btn-sm" data-go="Warehouse">View Warehouses</button>
				<button class="btn btn-default btn-sm" id="btn-view-logs">View migration log</button>
			</div>`;
		if (hasErrors) {
			html += `<p class="text-muted small" style="margin-top:12px;">
				To fix the failed records, open the migration log to see why each one failed,
				correct it in your Tally export, then upload again — the records that already
				imported will simply be skipped.</p>`;
		}
		html += `<div style="margin-top:16px;">
				<button id="btn-restart" class="btn btn-default btn-sm">↺ Migrate another file</button>
			</div></div>`;

		$("#results-section").html(html).show();

		// Wire next-step buttons
		$("#results-section [data-go]").on("click", (e) => {
			frappe.set_route("List", $(e.currentTarget).attr("data-go"));
		});
		$("#btn-view-logs").on("click", () => frappe.set_route("List", "Tally Migration Log"));
		$("#btn-restart").on("click", () => this.restart());
	}

	restart() {
		this.fileUrl = null;
		this.fileName = null;
		this.preview = null;
		$("#file-status").html("");
		$("#preview-box").hide().html("");
		$("#btn-next-upload").prop("disabled", true);
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
