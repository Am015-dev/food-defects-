/* Progressive enhancement only — every page works without this file.
   1. Filter forms: selects auto-submit (the "Εφαρμογή" button stays for no-JS).
   2. Shop-jump select navigates on change.
   3. [data-quickfilter] inputs narrow already-rendered rows as you type.
   4. th[data-sort] click-sorts the table body client-side. */

document.addEventListener("DOMContentLoaded", function () {
  // 1. Auto-submit filter selects.
  document.querySelectorAll("form.filters select").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });
  document.querySelectorAll("form.filters .js-hide").forEach(function (el) {
    el.style.display = "none";
  });

  // 2. Shop jump.
  document.querySelectorAll("select[data-jump]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.value) window.location.href = sel.dataset.jump.replace("SHOP", sel.value);
    });
    var btn = sel.parentElement.querySelector("button");
    if (btn) btn.style.display = "none";
  });

  // 3. Quick client-side narrowing of visible rows (does not replace the
  //    server-side search, which covers rows beyond this page).
  document.querySelectorAll("input[data-quickfilter]").forEach(function (input) {
    var target = document.querySelector(input.dataset.quickfilter);
    if (!target) return;
    var rows = target.matches("table")
      ? Array.prototype.slice.call(target.querySelectorAll("tbody tr"))
      : Array.prototype.slice.call(target.querySelectorAll("li"));
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      rows.forEach(function (row) {
        row.style.display = !q || row.textContent.toLowerCase().indexOf(q) !== -1 ? "" : "none";
      });
    });
  });

  // 4. Click-to-sort.
  document.querySelectorAll("th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function () {
      var table = th.closest("table");
      var tbody = table.querySelector("tbody");
      var index = Array.prototype.indexOf.call(th.parentElement.children, th);
      var dir = th.getAttribute("data-sort") === "asc" ? "desc" : "asc";
      table.querySelectorAll("th[data-sort]").forEach(function (other) {
        other.setAttribute("data-sort", "");
      });
      th.setAttribute("data-sort", dir);
      var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
      rows.sort(function (a, b) {
        var av = (a.children[index] || {}).textContent || "";
        var bv = (b.children[index] || {}).textContent || "";
        var an = parseFloat(av.replace(/[^\d,.-]/g, "").replace(",", "."));
        var bn = parseFloat(bv.replace(/[^\d,.-]/g, "").replace(",", "."));
        var cmp = !isNaN(an) && !isNaN(bn) ? an - bn : av.localeCompare(bv, "el");
        return dir === "asc" ? cmp : -cmp;
      });
      rows.forEach(function (row) { tbody.appendChild(row); });
    });
  });
});
