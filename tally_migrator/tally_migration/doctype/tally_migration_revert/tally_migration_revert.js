// Tally Migration Revert is written by a background job (see
// tally_migrator/migration/rollback.py: run_revert). While that job runs, the
// form colours its status and live-reloads when the job publishes completion, so
// the user can watch the undo finish without manually refreshing.
frappe.ui.form.on("Tally Migration Revert", {
	refresh(frm) {
		const colour = {
			Queued: "orange",
			"In Progress": "blue",
			Completed: "green",
			"Completed with Errors": "orange",
			Failed: "red",
		}[frm.doc.status];
		if (colour) {
			frm.page.set_indicator(__(frm.doc.status), colour);
		}
		if (["Queued", "In Progress"].includes(frm.doc.status)) {
			frm.dashboard.set_headline(
				__("The undo is running in the background. This page will refresh when it finishes.")
			);
		}
	},

	onload(frm) {
		// Reload when the background job reports this revert is done.
		frappe.realtime.on("tally_revert_updated", (data) => {
			if (data && data.revert === frm.doc.name) {
				frm.reload_doc();
			}
		});
	},
});
