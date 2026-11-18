/* Progressive Bootstrap classes for existing NEXUS ERP screens.
   This layer is presentation-only: it does not alter form names, values,
   routes, submission methods, or table data. */
(() => {
  "use strict";
  const enhance = root => {
    root.querySelectorAll?.("main .table-wrap").forEach(wrapper => {
      wrapper.classList.add("table-responsive");
      wrapper.querySelectorAll("table").forEach(table => {
        table.classList.add("table", "table-hover", "align-middle");
      });
    });
    root.querySelectorAll?.("main input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button])").forEach(input => {
      input.classList.add("form-control");
    });
    root.querySelectorAll?.("main select").forEach(select => {
      if (!select.multiple) select.classList.add("form-select");
    });
    root.querySelectorAll?.("main textarea").forEach(area => {
      area.classList.add("form-control");
    });
    root.querySelectorAll?.("main button, main input[type=submit], main a.button, main .button-link").forEach(button => {
      button.classList.add("btn");
      if (button.classList.contains("danger")) button.classList.add("btn-danger");
      else if (button.classList.contains("secondary")) button.classList.add("btn-outline-secondary");
      else button.classList.add("btn-primary");
    });
  };
  const start = () => enhance(document);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
  if (typeof MutationObserver !== "undefined") {
    const observer = new MutationObserver(records => {
      records.forEach(record => record.addedNodes.forEach(node => {
        if (node.nodeType === 1) {
          enhance(node);
          if (node.matches?.("main")) enhance(node);
        }
      }));
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
})();
