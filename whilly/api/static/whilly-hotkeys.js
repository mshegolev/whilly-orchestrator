/* Whilly hotkeys - drop in /static/whilly-hotkeys.js
 * Provides keyboard navigation matching the canonical operator dashboard.
 *
 * Bound keys (when focus is NOT in input/textarea/select):
 *   1-5 switch surface tabs (reads [data-surface-tab])
 *   /        focus #dashboard-filter
 *   q        stops the SSE live stream
 *   r        refreshes the dashboard
 *   p / P    POSTs /api/v1/admin/workers/pause
 *   R        POSTs /api/v1/admin/workers/resume
 *   j / k    move selection within actionable review rows
 *   a / x / c   dispatch review decision for the selected row
 *   Esc      clears row selection
 */
(function () {
  "use strict";

  const surfaceOrder = ["overview", "compliance", "plans_tasks", "workers", "events"];

  function isEditable(el) {
    if (!el) return false;
    const t = (el.tagName || "").toLowerCase();
    return t === "input" || t === "textarea" || t === "select" || el.isContentEditable;
  }

  function activeRows() {
    return Array.from(document.querySelectorAll('#review-gaps tbody tr[data-review-actionable="true"]')).filter(
      (row) => !row.hidden,
    );
  }

  function selectedRow() {
    return document.querySelector("#review-gaps tbody tr.review-row-selected");
  }

  function selectRow(row) {
    document.querySelectorAll("#review-gaps tbody tr[data-review-actionable]").forEach((candidate) => {
      candidate.classList.remove("review-row-selected");
      candidate.setAttribute("aria-selected", "false");
    });
    if (!row) return;
    row.classList.add("review-row-selected");
    row.setAttribute("aria-selected", "true");
    row.scrollIntoView({ block: "nearest" });
  }

  function stepRow(direction) {
    const rows = activeRows();
    if (!rows.length) return;
    const current = selectedRow();
    const index = current ? rows.indexOf(current) : -1;
    const nextIndex = Math.max(0, Math.min(rows.length - 1, index + direction));
    selectRow(rows[nextIndex]);
  }

  function review(decision) {
    const row = selectedRow();
    if (!row) return;
    const ev = new CustomEvent("whilly:reviewDecision", {
      bubbles: true,
      detail: { task_id: row.dataset.reviewTask || "", decision: decision, stage_id: row.dataset.reviewStage || "" },
    });
    document.body.dispatchEvent(ev);
  }

  function adminHeaders() {
    const token = (document.querySelector("#dashboard-admin-token")?.value || "").trim();
    const headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    return headers;
  }

  document.addEventListener("keydown", function (e) {
    if (isEditable(e.target) && e.key !== "Escape") return;
    const k = e.key;

    if (/^[1-5]$/.test(k)) {
      const surface = surfaceOrder[Number(k) - 1];
      const tab = document.querySelector('[data-surface-tab="' + surface + '"]');
      if (tab) {
        e.preventDefault();
        tab.click();
      }
      return;
    }

    if (k === "/") {
      const f = document.querySelector("#dashboard-filter");
      if (f) {
        e.preventDefault();
        f.focus();
      }
      return;
    }
    if (k === "Escape") return selectRow(null);

    if (k === "j" || k === "J") return stepRow(1);
    if (k === "k" || k === "K") return stepRow(-1);
    if (k === "a" || k === "A") return review("approved");
    if (k === "x" || k === "X") return review("rejected");
    if (k === "c" || k === "C") return review("changes_requested");

    if (k === "p" || k === "P") {
      fetch("/api/v1/admin/workers/pause", {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({}),
      }).catch(() => {});
      return;
    }
    if (k === "R") {
      fetch("/api/v1/admin/workers/resume", { method: "POST", headers: adminHeaders() }).catch(() => {});
      return;
    }
    if (k === "r") {
      window.htmx && htmx.ajax("GET", "/", { target: "body", swap: "outerHTML" });
      return;
    }
    if (k === "q") {
      document.body.dataset.dashboardState = "stopped";
      window.htmx && htmx.trigger(document.body, "whilly:liveStop");
      return;
    }
  });
})();
