/* Progressive enhancement only — every page works without this file.
   1. Filter forms: selects auto-submit (the "Εφαρμογή" button stays for no-JS).
   2. Shop-jump select navigates on change.
   3. [data-quickfilter] inputs narrow already-rendered rows as you type.
   4. th[data-sort] click-sorts the table body client-side.
   5. Stat count-up (vendor/countUp.umd.js).
   6. Scroll reveals (vendor/aos.js).
   7. Thumbnail fade-in.
   8. Broken thumbnail removal. */

document.addEventListener("DOMContentLoaded", function () {
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
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

  // 5. Stat count-up. Numbers are server-rendered, so no-JS and script
  //    load failure both keep the real value; separator "" keeps the
  //    final frame byte-identical to the server-rendered text.
  if (!reduceMotion && window.countUp) {
    document.querySelectorAll(".stat b").forEach(function (el) {
      var end = parseInt(el.textContent.replace(/\D/g, ""), 10);
      if (isNaN(end)) return;
      var counter = new countUp.CountUp(el, end, { startVal: 0, duration: 0.8, separator: "" });
      if (!counter.error) counter.start();
    });
  }

  // 6. Scroll reveals. disable under reduced motion; the CSS overrides in
  //    style.css also force [data-aos] visible for no-JS/reduced-motion.
  if (window.AOS) {
    AOS.init({ once: true, duration: 400, offset: 40, easing: "ease-out-cubic", disable: reduceMotion });
  }

  // 7. Fade thumbnails in as they finish loading. Cached images
  //    (img.complete) skip it so back-navigation doesn't re-fade.
  if (!reduceMotion) {
    document.querySelectorAll(".thumb img").forEach(function (img) {
      if (img.complete) return;
      img.classList.add("img-loading");
      function reveal() { img.classList.remove("img-loading"); }
      img.addEventListener("load", reveal);
      img.addEventListener("error", reveal);
    });
  }

  // 8. Remove a broken thumbnail (e.g. the CDN's HTTP 400 for an item
  //    with no image) so the wrapping span's cart-glyph placeholder
  //    shows through. This used to be an inline onerror= attribute;
  //    moved here so the CSP's script-src doesn't need 'unsafe-inline'.
  //    Runs unconditionally -- this is graceful degradation, not
  //    animation, so it isn't gated behind reduceMotion.
  document.querySelectorAll(".thumb img").forEach(function (img) {
    if (img.complete && img.naturalWidth === 0) {
      img.remove();
      return;
    }
    img.addEventListener("error", function () { img.remove(); });
  });
});
