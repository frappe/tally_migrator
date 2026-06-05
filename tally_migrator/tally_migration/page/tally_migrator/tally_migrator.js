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
		this.render();
	}

	render() {
		$(this.wrapper).find(".page-content").html(`
			<div class="container" style="max-width:640px; padding-top: 20px;">

				<!-- Step 1: Connection -->
				<div id="section-connect">
					<h5 class="text-muted uppercase" style="font-size:11px; letter-spacing:1px;">STEP 1 OF 3</h5>
					<h4>Connect to Tally</h4>
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
						<button id="btn-next-1" class="btn btn-primary btn-sm">Next →</button>
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

		this.bindEvents();
	}

	bindEvents() {
		const $ = window.$;

		// Step 1 — test connection
		$("#btn-test").on("click", () => this.testConnection());

		// Step 1 — diagnostic: dump raw company XML
		$("#btn-debug").on("click", (e) => {
			e.preventDefault();
			this.showDebugXml();
		});

		// Step 1 → Step 2
		$("#btn-next-1").on("click", () => {
			if (!$("#tally-company").val()) {
				frappe.msgprint("Please select a Tally company.");
				return;
			}
			this.loadERPNextCompanies();
			this.show("section-configure");
		});

		// Step 2 → Step 1
		$("#btn-back-2").on("click", () => this.show("section-connect"));

		// Step 2 → Step 3
		$("#btn-next-2").on("click", () => {
			if (!$("#erpnext-company").val()) {
				frappe.msgprint("Please select an ERPNext company.");
				return;
			}
			const tally = $("#tally-company").val();
			const erpnext = $("#erpnext-company").val();
			$("#run-subtitle").text(
				`Migrating masters from "${tally}" → "${erpnext}"`
			);
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
			$("#conn-status").html("");
			$("#company-section").hide();
			$("#progress-section").hide();
			$("#results-section").hide();
			$("#error-section").hide();
			$("#progress-bar").css("width", "0%").text("0%");
			this.show("section-connect");
		});
	}

	show(sectionId) {
		["section-connect", "section-configure", "section-run"].forEach((id) => {
			$("#" + id).hide();
		});
		$("#" + sectionId).show();
	}

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
					$("#conn-status").html(
						'<span class="indicator green">Connected</span>'
					);
					const $select = $("#tally-company").empty();
					(result.companies || []).forEach((c) => {
						$select.append(`<option value="${c}">${c}</option>`);
					});
					$("#company-section").show();
				} else {
					$("#conn-status").html(
						'<span class="indicator red">Cannot connect</span>'
					);
					$("#company-section").hide();
					frappe.msgprint(
						`Could not reach Tally on ${host}:${port}. ` +
						"Make sure Tally is running and the HTTP server is enabled."
					);
				}
			},
			error: () => {
				$("#btn-test").prop("disabled", false).text("Test Connection");
				$("#conn-status").html(
					'<span class="indicator red">Error</span>'
				);
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
				const parsed = (m.parsed || []);
				$("#debug-output").text(
					"Parsed companies: " +
						(parsed.length ? JSON.stringify(parsed) : "(none)") +
						"\n\n--- Raw XML ---\n" +
						(m.raw || "(empty)")
				);
			},
			error: () => {
				$("#debug-output").text("Request failed — is Tally reachable on " + host + ":" + port + "?");
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

	runMigration() {
		const host    = $("#tally-host").val() || "localhost";
		const port    = parseInt($("#tally-port").val()) || 9000;
		const tally   = $("#tally-company").val();
		const erpnext = $("#erpnext-company").val();

		$("#btn-run").prop("disabled", true);
		$("#btn-back-3").prop("disabled", true);
		$("#error-section").hide();
		$("#results-section").hide();
		$("#progress-section").show();

		// Listen for realtime progress events
		frappe.realtime.on("progress", (data) => {
			if (data.title !== "Tally Masters Migration") return;
			const pct = data.percent || 0;
			$("#progress-bar").css("width", pct + "%").text(pct + "%");
			$("#progress-desc").text(data.description || "");
		});

		frappe.call({
			method: "tally_migrator.api.run_masters_migration",
			args: {
				tally_host:      host,
				tally_port:      port,
				tally_company:   tally,
				erpnext_company: erpnext,
			},
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
				const msg = err?.message || "An unexpected error occurred.";
				$("#error-section").text("Migration failed: " + msg).show();
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
