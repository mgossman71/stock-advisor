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
