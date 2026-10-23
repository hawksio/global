// Global Marketing: small progressive enhancements. Every page works without JavaScript.
(function () {
  "use strict";
  var root = document.documentElement;
  try { var t = localStorage.getItem("gm-theme"); if (t) root.setAttribute("data-theme", t); } catch (e) {}

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-theme-toggle]");
    if (el) {
      var dark = root.getAttribute("data-theme") === "dark" ||
        (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
      var next = dark ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("gm-theme", next); } catch (e) {}
      return;
    }
    var menu = ev.target.closest("[data-menu-toggle]");
    if (menu) { document.querySelector(".sidebar").classList.toggle("open"); return; }
    var close = ev.target.closest("[data-dismiss]");
    if (close) { close.parentElement.remove(); return; }
    var tab = ev.target.closest("[data-preview-mode]");
    if (tab) {
      var frame = document.getElementById(tab.getAttribute("data-target"));
      if (frame) frame.classList.toggle("mobile", tab.getAttribute("data-preview-mode") === "mobile");
    }
  });

  // Confirmation dialogs for destructive actions: <form data-confirm="Delete 3 contacts?">
  var dialog = null;
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var submitter = ev.submitter;
    var msg = (submitter && submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (!msg || form.dataset.confirmed === "1") return;
    ev.preventDefault();
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.innerHTML = '<h3 id="dlg-title">Please confirm</h3><p id="dlg-msg"></p>' +
        '<div class="actions" style="justify-content:flex-end"><button type="button" class="btn" value="cancel">Cancel</button>' +
        '<button type="button" class="btn danger" value="ok">Yes, continue</button></div>';
      dialog.setAttribute("aria-labelledby", "dlg-title");
      document.body.appendChild(dialog);
    }
    dialog.querySelector("#dlg-msg").textContent = msg;
    dialog.querySelector('[value="cancel"]').onclick = function () { dialog.close(); };
    dialog.querySelector('[value="ok"]').onclick = function () {
      dialog.close();
      form.dataset.confirmed = "1";
      if (submitter && submitter.name) {
        var h = document.createElement("input");
        h.type = "hidden"; h.name = submitter.name; h.value = submitter.value; form.appendChild(h);
      }
      form.submit();
    };
    dialog.showModal();
    dialog.querySelector('[value="cancel"]').focus();
  });

  // Bulk selection in tables
  function refreshBulk(table) {
    var boxes = table.querySelectorAll("input[name=ids]");
    var n = 0;
    boxes.forEach(function (b) { if (b.checked) n++; });
    var bar = document.getElementById(table.getAttribute("data-bulk"));
    if (bar) {
      bar.classList.toggle("on", n > 0);
      var c = bar.querySelector("[data-count]");
      if (c) c.textContent = n;
    }
  }
  document.addEventListener("change", function (ev) {
    var all = ev.target.closest("[data-select-all]");
    var table = ev.target.closest("[data-bulk]");
    if (all && table) table.querySelectorAll("input[name=ids]").forEach(function (b) { b.checked = all.checked; });
    if (table) refreshBulk(table);
    var auto = ev.target.closest("[data-autosubmit]");
    if (auto) auto.form.submit();
  });

  // Job progress polling: <div data-job="/jobs/12.json">
  document.querySelectorAll("[data-job]").forEach(function (box) {
    var url = box.getAttribute("data-job");
    function tick() {
      fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var pct = j.total ? Math.min(100, Math.round(100 * j.progress / j.total)) : (j.status === "completed" ? 100 : 5);
          var bar = box.querySelector(".progress > span");
          if (bar) bar.style.width = pct + "%";
          var txt = box.querySelector("[data-job-text]");
          if (txt) txt.textContent = (j.message || j.status) + (j.total ? " (" + pct + "%)" : "");
          if (j.status === "queued" || j.status === "running") setTimeout(tick, 1500);
          else if (box.hasAttribute("data-reload")) window.location.reload();
        })
        .catch(function () { setTimeout(tick, 5000); });
    }
    tick();
  });

  // Copy buttons
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-copy]");
    if (!b) return;
    var src = document.getElementById(b.getAttribute("data-copy"));
    if (src && navigator.clipboard) {
      navigator.clipboard.writeText(src.value || src.textContent).then(function () { b.textContent = "Copied"; });
    }
  });

  // Insert personalisation field into the focused textarea
  var lastField = null;
  document.addEventListener("focusin", function (ev) { if (ev.target.matches("textarea, input[type=text]")) lastField = ev.target; });
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-insert]");
    if (!b || !lastField) return;
    ev.preventDefault();
    var v = b.getAttribute("data-insert"), s = lastField.selectionStart || 0, e = lastField.selectionEnd || 0;
    lastField.value = lastField.value.slice(0, s) + v + lastField.value.slice(e);
    lastField.focus();
    lastField.selectionStart = lastField.selectionEnd = s + v.length;
  });

  // Compose: only show the address box when "Type email addresses" is chosen
  var toMode = document.getElementById("to_mode");
  function syncTo() {
    document.querySelectorAll("[data-show-when]").forEach(function (el) {
      el.hidden = toMode.value !== el.getAttribute("data-show-when");
    });
  }
  if (toMode) { toMode.addEventListener("change", syncTo); syncTo(); }

  // Auto-hide toasts
  setTimeout(function () {
    document.querySelectorAll(".toast.success").forEach(function (t) { t.remove(); });
  }, 7000);
})();
