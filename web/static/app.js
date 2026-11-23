/* Live "cost of this trade" readout on the order tickets.
   Local file, zero dependencies: unlike the CDN scripts this one must
   always work (the cost number is the point of the ticket). */
(function () {
  "use strict";
  var fmt = function (n) {
    return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  document.querySelectorAll("form[data-cost]").forEach(function (form) {
    var out = document.getElementById(form.getAttribute("data-cost"));
    if (!out) return;
    var qty = form.querySelector('[name="qty"]');
    var price = form.querySelector('[name="price"]');
    var symbol = form.querySelector('[name="symbol"]');
    function unitPrice() {
      if (price && price.value) return parseFloat(price.value);
      if (symbol) {
        var opt = symbol.selectedOptions[0];
        var c = opt && opt.getAttribute("data-close");
        if (c) return parseFloat(c);
      }
      return NaN;
    }
    var asofOut = document.getElementById(form.getAttribute("data-asof") || "");
    var session = form.getAttribute("data-session") || "";
    function update() {
      var q = parseFloat(qty && qty.value);
      var p = unitPrice();
      out.textContent = (q > 0 && p > 0) ? "$" + fmt(q * p) + "  (" + fmt(q) + " × " + fmt(p) + ")" : "—";
      if (asofOut && symbol) {
        // ISO dates compare correctly as strings.
        var opt = symbol.selectedOptions[0];
        var asof = opt && opt.getAttribute("data-asof");
        asofOut.textContent = !asof ? "" : "Price as of " + asof +
          (asof < session ? " (old — a live quote is fetched when you submit)." : ".");
      }
    }
    [qty, price, symbol].forEach(function (el) {
      if (el) el.addEventListener("input", update);
    });
    update();
  });
})();

/* "Back" links.  Every page without one (the lists: Research, Screen,
   Dashboard...) records itself for this tab; a stock page's Back goes to
   that page.  document.referrer was not reliable enough on phones (a
   second stock opened from Research went "back" to the Dashboard), and
   history.back() alone can't tell a chart-range change from a real
   arrival.  The plain href "/" still works with no JS or storage. */
(function () {
  "use strict";
  var KEY = "sa-back";
  var links = document.querySelectorAll("a[data-back]");
  var here = location.pathname + location.search;
  if (!links.length) {
    // pageshow also fires when a swipe-back restores the page from cache.
    window.addEventListener("pageshow", function () {
      try { sessionStorage.setItem(KEY, here); } catch (e) { /* private mode */ }
    });
    return;
  }
  var target = null;
  try { target = sessionStorage.getItem(KEY); } catch (e) { /* use the href */ }
  if (!target || target.charAt(0) !== "/" || target.charAt(1) === "/") return;
  links.forEach(function (a) {
    a.setAttribute("href", target);
    a.addEventListener("click", function (ev) {
      // Came straight from that page: step back, keeping its scroll position.
      var ref;
      try { ref = new URL(document.referrer); } catch (e) { return; }
      if (ref.origin === location.origin && ref.pathname + ref.search === target &&
          history.length > 1) {
        ev.preventDefault();
        history.back();
      }
    });
  });
})();
