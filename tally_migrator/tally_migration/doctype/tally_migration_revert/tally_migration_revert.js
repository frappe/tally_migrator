// Tally Migration Revert is written by a background job (see
// tally_migrator/migration/rollback.py: run_revert). While that job runs, the
// form colours its status and live-reloads when the job finishes, so the user can
// watch the undo complete without manually refreshing.
const TM_REVERT_TERMINAL = ["Completed", "Completed with Errors", "Failed"];

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
		if (!TM_REVERT_TERMINAL.includes(frm.doc.status)) {
			frm.dashboard.set_headline(
				__("The undo is running in the background. This page will refresh when it finishes.")
			);
			tm_start_status_poll(frm);
		} else {
			tm_stop_status_poll(frm);
		}
	},

	onload(frm) {
		// Fast path: reload the moment the background job reports this revert is done.
		frappe.realtime.on("tally_revert_updated", (data) => {
			if (data && data.revert === frm.doc.name) {
				frm.reload_doc();
			}
		});
	},
});

// Reliable fallback: realtime is best-effort, so a page that missed the completion
// event (a backgrounded tab, a socket reconnect, an incognito session) would otherwise
// sit on "Queued" forever while the job has actually finished. Poll the status and
// reload once it reaches a terminal state. Mirrors the wizard's step-5 poll fallback.
function tm_start_status_poll(frm) {
	tm_stop_status_poll(frm);
	frm._tm_revert_poll = setInterval(() => {
		// Self-terminate if the user navigated away (form DOM detached), so the poll
		// never leaks past the page it belongs to.
		if (!frm.page || !frm.page.wrapper || !document.body.contains(frm.page.wrapper[0])) {
			tm_stop_status_poll(frm);
			return;
		}
		frappe.db
			.get_value("Tally Migration Revert", frm.doc.name, "status")
			.then((r) => {
				const status = r && r.message && r.message.status;
				if (status && TM_REVERT_TERMINAL.includes(status)) {
					tm_stop_status_poll(frm);
					frm.reload_doc();
				}
			});
	}, 5000);
}

function tm_stop_status_poll(frm) {
	if (frm._tm_revert_poll) {
		clearInterval(frm._tm_revert_poll);
		frm._tm_revert_poll = null;
	}
}
