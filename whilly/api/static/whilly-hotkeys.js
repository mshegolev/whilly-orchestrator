/* Whilly hotkeys — drop in /static/whilly-hotkeys.js
 * Provides keyboard navigation matching the Tui dashboard.
 *
 * Bound keys (when focus is NOT in input/textarea/select):
 *   1-7      switch surface tabs (reads data-key on the <nav.tabs> children)
 *   /        focus first .panel .inp
 *   q        stops the SSE live stream (htmx.trigger body, "whilly:liveStop")
 *   r        triggers fragment refresh of #tasks + #workers
 *   p / P    POSTs /admin/workers/pause
 *   R        POSTs /admin/workers/resume
 *   j / k    move selection within the first .tbl tbody
 *   a / x / c   POSTs review decision for the selected row
 *   t        cycles data-theme on <html>
 *   Esc      clears row selection
 */
(function () {
  "use strict";
  function isEditable(el) {
    if (!el) return false;
    const t = (el.tagName || "").toLowerCase();
    return t === "input" || t === "textarea" || t === "select" || el.isContentEditable;
  }
  function activeRows() {
    const tbl = document.querySelector(".panel .tbl tbody");
    return tbl ? Array.from(tbl.querySelectorAll("tr")) : [];
  }
  function selectedRow() {
    return document.querySelector(".panel .tbl tbody tr.selected");
  }
  function selectRow(tr) {
    activeRows().forEach((r) => r.classList.remove("selected"));
    if (tr) {
      tr.classList.add("selected");
      tr.scrollIntoView({ block: "nearest" });
    }
  }
  function cycleTheme() {
    const cur = document.documentElement.getAttribute("data-theme") || "auto";
    const next = cur === "light" ? "dark" : cur === "dark" ? "auto" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("whilly:theme", next); } catch (_) {}
  }
  function review(decision) {
    const row = selectedRow();
    if (!row) return;
    const id = row.id.replace(/^task-/, "");
    const ev = new CustomEvent("whilly:reviewDecision", {
      bubbles: true, detail: { task_id: id, decision: decision }
    });
    document.body.dispatchEvent(ev);
  }

  // Restore theme
  try {
    const saved = localStorage.getItem("whilly:theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (_) {}

  document.addEventListener("keydown", function (e) {
    if (isEditable(e.target) && e.key !== "Escape") return;
    const k = e.key;

    if (/^[1-7]$/.test(k)) {
      const tab = document.querySelector('.tabs [data-key="' + k + '"]');
      if (tab) { e.preventDefault(); tab.click(); }
      return;
    }

    if (k === "/") {
      const f = document.querySelector(".panel .inp, .panel input");
      if (f) { e.preventDefault(); f.focus(); }
      return;
    }
    if (k === "t") return cycleTheme();
    if (k === "Escape") return selectRow(null);

    if (k === "j" || k === "J") {
      const rows = activeRows(); if (!rows.length) return;
      const cur = selectedRow();
      const idx = cur ? rows.indexOf(cur) : -1;
      selectRow(rows[Math.min(rows.length - 1, idx + 1)]);
      return;
    }
    if (k === "k" || k === "K") {
      const rows = activeRows(); if (!rows.length) return;
      const cur = selectedRow();
      const idx = cur ? rows.indexOf(cur) : rows.length;
      selectRow(rows[Math.max(0, idx - 1)]);
      return;
    }
    if (k === "a" || k === "A") return review("approved");
    if (k === "x" || k === "X") return review("rejected");
    if (k === "c" || k === "C") return review("changes_requested");

    if (k === "p" || k === "P") {
      fetch("/admin/workers/pause", { method: "POST" }).catch(() => {});
      return;
    }
    if (k === "R") {
      fetch("/admin/workers/resume", { method: "POST" }).catch(() => {});
      return;
    }
    if (k === "r") {
      window.htmx && htmx.trigger("#tasks", "whilly:refresh");
      window.htmx && htmx.trigger("#workers", "whilly:refresh");
      return;
    }
    if (k === "q") {
      window.htmx && htmx.trigger(document.body, "whilly:liveStop");
      return;
    }
  });
})();
