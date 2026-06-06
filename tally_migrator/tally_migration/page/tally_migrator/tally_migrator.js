frappe.pages["tally-migrator"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Tally Migrator",
		single_column: true,
	});

	new TallyMigratorPage(page, wrapper);
};

class TallyMigratorPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.companies = [];
		// "file" (recommended, offline export) | "live" (direct port-9000 link)
		this.sourceMode = "file";
		this.fileUrl = null;
		this.render();
	}

	render() {
		$(this.wrapper).find(".page-content").html(`
			<div class="container" style="max-width:640px; padding-top: 20px;">

				<!-- Step 1: Choose data source -->
				<div id="section-source">
					<h5 class="text-muted uppercase" style="font-size:11px; letter-spacing:1px;">STEP 1 OF 3</h5>
					<h4>Choose how to bring in your Tally data</h4>
					<p class="text-muted">Pick the method that fits your setup. You can switch any time before starting.</p>

					<div class="row" style="margin-bottom:8px;">
						<div class="col-sm-6">
							<div id="card-file" class="src-card src-card-active">
								<div style="display:flex; align-items:center; gap:8px;">
									<input type="radio" name="src" value="file" checked />
									<strong>Upload export file</strong>
									<span class="indicator-pill green" style="font-size:10px;">RECOMMENDED</span>
								</div>
								<p class="text-muted" style="font-size:12px; margin:6px 0 0 24px;">
									Export an XML file from Tally and upload it here. Works everywhere —
									including hosted ERPNext — and gives a saved, repeatable record.
								</p>
							</div>
						</div>
						<div class="col-sm-6">
							<div id="card-live" class="src-card">
								<div style="display:flex; align-items:center; gap:8px;">
									<input type="radio" name="src" value="live" />
									<strong>Direct connection</strong>
								</div>
								<p class="text-muted" style="font-size:12px; margin:6px 0 0 24px;">
									Connect live to a running Tally on the same network (port 9000).
									Best for on-premise setups where ERPNext can reach Tally.
								</p>
							</div>
						</div>
					</div>

					<!-- File sub-panel -->
					<div id="panel-file" style="margin-top:16px;">
						<p class="text-muted">
							In <strong>Tally Prime</strong>: <strong>Gateway of Tally → Import/Export → Export</strong>,
							choose <em>Masters</em>, set format to <strong>XML</strong>, and export. Then upload that file below.
						</p>
						<button id="btn-pick-file" class="btn btn-default btn-sm">Upload Master Data XML</button>
						<span id="file-status" style="margin-left:12px;" class="text-muted"></span>
						<div style="margin-top:16px;">
							<button id="btn-next-file" class="btn btn-primary btn-sm" disabled>Next →</button>
						</div>
					</div>

					<!-- Live sub-panel -->
					<div id="panel-live" style="display:none; margin-top:16px;">
						<p class="text-muted">
							Open Tally and enable the HTTP server. In <strong>Tally Prime</strong>:
							<strong>F1 (Help) → Settings → Connectivity → Client/Server configuration</strong>,
							set <em>"TallyPrime acts as"</em> to <strong>Both</strong> (or Server) on port 9000.
							Make sure your company is open on screen.
						</p>
						<div class="form-group row">
							<div class="col-sm-8">
								<label class="control-label">Tally Host</label>
								<input id="tally-host" class="form-control" value="localhost" />
							</div>
							<div class="col-sm-4">
								<label class="control-label">Port</label>
								<input id="tally-port" class="form-control" value="9000" type="number" />
							</div>
						</div>
						<button id="btn-test" class="btn btn-default btn-sm">Test Connection</button>
						<a href="#" id="btn-debug" style="margin-left:12px; font-size:12px;">Show raw XML</a>
						<span id="conn-status" style="margin-left:12px;"></span>

						<div id="debug-section" style="display:none; margin-top:12px;">
							<label class="control-label" style="font-size:12px;">Raw response from Tally (diagnostic)</label>
							<pre id="debug-output" style="max-height:240px; overflow:auto; font-size:11px; background:#f8f8f8;"></pre>
						</div>

						<div id="company-section" style="display:none; margin-top:20px;">
							<div class="form-group">
								<label class="control-label">Tally Company</label>
								<select id="tally-company" class="form-control" style="max-width:360px;"></select>
							</div>
							<button id="btn-next-live" class="btn btn-primary btn-sm">Next →</button>
						</div>
					</div>
				</div>

				<!-- Step 2: Configure -->
				<div id="section-configure" style="display:none;">
					<h5 class="text-muted uppercase" style="font-size:11px; letter-spacing:1px;">STEP 2 OF 3</h5>
					<h4>Configure Target</h4>
					<p class="text-muted">Select the ERPNext company that will receive the migrated data.</p>
					<div class="form-group">
						<label class="control-label">ERPNext Company</label>
						<select id="erpnext-company" class="form-control" style="max-width:360px;"></select>
					</div>

					<div class="well well-sm" style="margin-top:16px; margin-bottom:20px;">
						<strong>Phase 1 — What will be migrated</strong>
						<ul class="list-unstyled" style="margin-top:8px; margin-bottom:0;">
							<li>✓ &nbsp;Customers — ledgers under Sundry Debtors</li>
							<li>✓ &nbsp;Suppliers — ledgers under Sundry Creditors</li>
							<li>✓ &nbsp;Items — all Stock Items with UOM mapping</li>
							<li>✓ &nbsp;Warehouses — all Godowns (parent-before-child)</li>
							<li>✓ &nbsp;Addresses — billing address per customer / supplier</li>
						</ul>
					</div>
					<p class="text-muted" style="font-size:12px;">
						Existing records are <strong>never overwritten</strong> — the migrator skips any record already present in ERPNext.
					</p>

					<button id="btn-back-2" class="btn btn-default btn-sm">← Back</button>
					&nbsp;
					<button id="btn-next-2" class="btn btn-primary btn-sm">Start Migration →</button>
				</div>

				<!-- Step 3: Run & Results -->
				<div id="section-run" style="display:none;">
					<h5 class="text-muted uppercase" style="font-size:11px; letter-spacing:1px;">STEP 3 OF 3</h5>
					<h4>Migration</h4>
					<p id="run-subtitle" class="text-muted"></p>

					<div id="progress-section" style="display:none; margin-bottom:20px;">
						<div class="progress" style="margin-bottom:6px;">
							<div id="progress-bar" class="progress-bar progress-bar-striped active" style="width:0%; min-width:2em;">0%</div>
						</div>
						<p id="progress-desc" class="text-muted" style="font-size:12px; margin:0;">Starting…</p>
					</div>

					<div id="results-section" style="display:none;">
						<div id="results-table"></div>
						<br>
						<button id="btn-view-logs" class="btn btn-default btn-sm">View Migration Logs</button>
						&nbsp;
						<button id="btn-restart" class="btn btn-default btn-sm">← Start Over</button>
					</div>

					<div id="error-section" style="display:none;" class="alert alert-danger"></div>

					<button id="btn-back-3" class="btn btn-default btn-sm">← Back</button>
					&nbsp;
					<button id="btn-run" class="btn btn-primary btn-sm">▶ Run Migration</button>
				</div>

			</div>
		`);

		this.injectStyles();
		this.bindEvents();
	}

	injectStyles() {
		if (document.getElementById("tally-migrator-styles")) return;
		const css = `
			.src-card { border:1px solid var(--border-color, #d1d8dd); border-radius:8px;
				padding:12px; cursor:pointer; height:100%; transition:border-color .15s, box-shadow .15s; }
			.src-card:hover { border-color:#7575ff; }
			.src-card-active { border-color:#5e64ff; box-shadow:0 0 0 1px #5e64ff inset; }
		`;
		$("<style>", { id: "tally-migrator-styles", text: css }).appendTo("head");
	}

	bindEvents() {
		const $ = window.$;

		// Step 1 — source selection
		$("#card-file").on("click", () => this.selectSource("file"));
		$("#card-live").on("click", () => this.selectSource("live"));

		// File path — upload + advance
		$("#btn-pick-file").on("click", () => this.pickFile());
		$("#btn-next-file").on("click", () => {
			if (!this.fileUrl) {
				frappe.msgprint("Please upload a Master Data XML file first.");
				return;
			}
			this.proceedToConfigure();
		});

		// Live path — test connection
		$("#btn-test").on("click", () => this.testConnection());
		$("#btn-debug").on("click", (e) => {
			e.preventDefault();
			this.showDebugXml();
		});
		$("#btn-next-live").on("click", () => {
			if (!$("#tally-company").val()) {
				frappe.msgprint("Please select a Tally company.");
				return;
			}
			this.proceedToConfigure();
		});

		// Step 2 → Step 1
		$("#btn-back-2").on("click", () => this.show("section-source"));

		// Step 2 → Step 3
		$("#btn-next-2").on("click", () => {
			if (!$("#erpnext-company").val()) {
				frappe.msgprint("Please select an ERPNext company.");
				return;
			}
			const erpnext = $("#erpnext-company").val();
			const sourceLabel =
				this.sourceMode === "file"
					? "uploaded file"
					: `"${$("#tally-company").val()}" (live)`;
			$("#run-subtitle").text(`Migrating masters from ${sourceLabel} → "${erpnext}"`);
			this.show("section-run");
		});

		// Step 3 → Step 2
		$("#btn-back-3").on("click", () => this.show("section-configure"));

		// Run migration
		$("#btn-run").on("click", () => this.runMigration());

		// View logs
		$("#btn-view-logs").on("click", () => {
			frappe.set_route("List", "Tally Migration Log");
		});

		// Start over
		$("#btn-restart").on("click", () => {
			// Reset live-connection state
			$("#conn-status").html("");
			$("#company-section").hide();
			// Reset file-upload state
			this.fileUrl = null;
			$("#file-status").html("");
			$("#btn-next-file").prop("disabled", true);
			// Reset run/results state
			$("#progress-section").hide();
			$("#results-section").hide();
			$("#error-section").hide();
			$("#progress-bar").css("width", "0%").text("0%");
			$("#btn-run").show().prop("disabled", false);
			$("#btn-back-3").show().prop("disabled", false);
			this.show("section-source");
		});
	}

	// ── Source selection ──────────────────────────────────────────────────────

	selectSource(mode) {
		this.sourceMode = mode;
		$("#card-file").toggleClass("src-card-active", mode === "file");
		$("#card-live").toggleClass("src-card-active", mode === "live");
		$(`input[name="src"][value="${mode}"]`).prop("checked", true);
		$("#panel-file").toggle(mode === "file");
		$("#panel-live").toggle(mode === "live");
	}

	pickFile() {
		new frappe.ui.FileUploader({
			folder: "Home/Attachments",
			restrictions: { allowed_file_types: [".xml", "text/xml", "application/xml"] },
			on_success: (file_doc) => {
				this.fileUrl = file_doc.file_url;
				$("#file-status").html(
					`<span class="indicator green">${frappe.utils.escape_html(
						file_doc.file_name || file_doc.file_url
					)}</span>`
				);
				$("#btn-next-file").prop("disabled", false);
			},
		});
	}

	proceedToConfigure() {
		this.loadERPNextCompanies();
		this.show("section-configure");
	}

	// ── Navigation ────────────────────────────────────────────────────────────

	show(sectionId) {
		["section-source", "section-configure", "section-run"].forEach((id) => {
			$("#" + id).hide();
		});
		$("#" + sectionId).show();
	}

	// ── Live connection ───────────────────────────────────────────────────────

	testConnection() {
		const host = $("#tally-host").val() || "localhost";
		const port = parseInt($("#tally-port").val()) || 9000;

		$("#btn-test").prop("disabled", true).text("Testing…");
		$("#conn-status").html("");

		frappe.call({
			method: "tally_migrator.api.ping_tally",
			args: { tally_host: host, tally_port: port },
			callback: (r) => {
				$("#btn-test").prop("disabled", false).text("Test Connection");
				const result = r.message;
				if (result && result.reachable) {
					$("#conn-status").html('<span class="indicator green">Connected</span>');
					const $select = $("#tally-company").empty();
					(result.companies || []).forEach((c) => {
						$select.append(`<option value="${c}">${c}</option>`);
					});
					$("#company-section").show();
				} else {
					$("#conn-status").html('<span class="indicator red">Cannot connect</span>');
					$("#company-section").hide();
					frappe.msgprint(
						`Could not reach Tally on ${host}:${port}. ` +
							"Make sure Tally is running and the HTTP server is enabled."
					);
				}
			},
			error: () => {
				$("#btn-test").prop("disabled", false).text("Test Connection");
				$("#conn-status").html('<span class="indicator red">Error</span>');
			},
		});
	}

	showDebugXml() {
		const host = $("#tally-host").val() || "localhost";
		const port = parseInt($("#tally-port").val()) || 9000;

		$("#debug-section").show();
		$("#debug-output").text("Fetching…");

		frappe.call({
			method: "tally_migrator.api.debug_company_xml",
			args: { tally_host: host, tally_port: port },
			callback: (r) => {
				const m = r.message || {};
				const parsed = m.parsed || [];
				$("#debug-output").text(
					"Parsed companies: " +
						(parsed.length ? JSON.stringify(parsed) : "(none)") +
						"\n\n--- Raw XML ---\n" +
						(m.raw || "(empty)")
				);
			},
			error: () => {
				$("#debug-output").text(
					"Request failed — is Tally reachable on " + host + ":" + port + "?"
				);
			},
		});
	}

	loadERPNextCompanies() {
		frappe.call({
			method: "frappe.client.get_list",
			args: { doctype: "Company", fields: ["name"], limit_page_length: 100 },
			callback: (r) => {
				const $select = $("#erpnext-company").empty();
				$select.append('<option value="">Select company…</option>');
				(r.message || []).forEach((c) => {
					$select.append(`<option value="${c.name}">${c.name}</option>`);
				});
			},
		});
	}

	// ── Run ───────────────────────────────────────────────────────────────────

	runMigration() {
		const erpnext = $("#erpnext-company").val();

		$("#btn-run").prop("disabled", true);
		$("#btn-back-3").prop("disabled", true);
		$("#error-section").hide();
		$("#results-section").hide();
		$("#progress-section").show();

		// Listen for realtime progress events (same title for both sources)
		frappe.realtime.on("progress", (data) => {
			if (data.title !== "Tally Masters Migration") return;
			const pct = data.percent || 0;
			$("#progress-bar").css("width", pct + "%").text(pct + "%");
			$("#progress-desc").text(data.description || "");
		});

		// Branch the backend call on the chosen source.
		const call =
			this.sourceMode === "file"
				? {
						method: "tally_migrator.api.run_masters_migration_from_file",
						args: { file_url: this.fileUrl, erpnext_company: erpnext },
				  }
				: {
						method: "tally_migrator.api.run_masters_migration",
						args: {
							tally_host: $("#tally-host").val() || "localhost",
							tally_port: parseInt($("#tally-port").val()) || 9000,
							tally_company: $("#tally-company").val(),
							erpnext_company: erpnext,
						},
				  };

		frappe.call({
			...call,
			callback: (r) => {
				frappe.realtime.off("progress");
				$("#btn-run").prop("disabled", false);
				$("#btn-back-3").prop("disabled", false);
				$("#progress-bar")
					.removeClass("active progress-bar-striped")
					.css("width", "100%")
					.text("100%");

				const summary = r.message;
				if (summary) {
					this.renderResults(summary);
					$("#btn-run").hide();
					$("#btn-back-3").hide();
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
							`<br><span style="font-size:12px;">Nothing was left half-done — already-imported records are kept and safe to re-run. ` +
							`Open <a href="#" class="err-logs-link">Migration Logs</a> to see exactly what failed.</span>`
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
		let html = `
			<div class="alert ${hasErrors ? "alert-warning" : "alert-success"}">
				${hasErrors ? "⚠ Migration completed with some errors." : "✓ All records migrated successfully."}
			</div>
			<table class="table table-bordered table-condensed" style="margin-top:12px;">
				<thead>
					<tr>
						<th>Entity</th>
						<th class="text-right">Created</th>
						<th class="text-right">Skipped</th>
						<th class="text-right">Failed</th>
					</tr>
				</thead>
				<tbody>
		`;
		for (const [label, result] of Object.entries(summary)) {
			html += `
				<tr>
					<td>${label}</td>
					<td class="text-right text-success"><strong>${result.created}</strong></td>
					<td class="text-right text-muted">${result.skipped}</td>
					<td class="text-right ${result.failed > 0 ? "text-danger" : "text-muted"}">
						${result.failed > 0 ? `<strong>${result.failed}</strong>` : result.failed}
					</td>
				</tr>
			`;
		}
		html += `</tbody></table>`;

		$("#results-table").html(html);
		$("#results-section").show();
	}
}
