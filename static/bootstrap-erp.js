/* Progressive Bootstrap classes for existing NEXUS ERP screens.
   Presentation-only: keep names, values, routes, methods and submitted data unchanged. */
(() => {
  "use strict";
  const enhance = root => {
    if (!root || !root.querySelectorAll) return;
    /* Wrap legacy tables that were not authored inside a responsive container.
       Moving a table does not change its fields, links, form submissions or data. */
    root.querySelectorAll("main table").forEach(table => {
      const parent = table.parentElement;
      if (parent && !parent.classList.contains("table-wrap") &&
          !parent.classList.contains("table-responsive") &&
          !parent.classList.contains("tax-summary-table-wrap")) {
        const wrapper = document.createElement("div");
        wrapper.className = "table-wrap table-responsive erp-table-scroll";
        parent.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
    });
    root.querySelectorAll("main .table-wrap, main .table-responsive, main .tax-summary-table-wrap").forEach(wrapper => {
      wrapper.classList.add("table-responsive");
    });
    root.querySelectorAll("main table").forEach(table => {
      table.classList.add("table", "table-hover", "align-middle", "erp-table");
      if (table.querySelector("thead")) table.classList.add("table-striped");
      const parent = table.parentElement;
      if (parent && (parent.classList.contains("table-wrap") || parent.classList.contains("table-responsive"))) {
        parent.classList.add("erp-table-scroll");
      }
    });
    root.querySelectorAll("main input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=file])").forEach(input => {
      input.classList.add("form-control");
    });
    root.querySelectorAll("main input[type=file]").forEach(input => input.classList.add("form-control"));
    root.querySelectorAll("main select").forEach(select => {
      select.classList.add(select.multiple ? "form-select" : "form-select");
    });
    root.querySelectorAll("main textarea").forEach(area => area.classList.add("form-control"));
    root.querySelectorAll("main button, main input[type=submit], main input[type=button], main a.button, main .button-link").forEach(control => {
      control.classList.add("btn");
      if (control.classList.contains("danger")) control.classList.add("btn-danger");
      else if (control.classList.contains("secondary")) control.classList.add("btn-outline-secondary");
      else if (!control.classList.contains("btn-link")) control.classList.add("btn-primary");
    });
    root.querySelectorAll("main .table-actions").forEach(actions => actions.classList.add("d-flex", "flex-wrap", "align-items-center", "gap-2"));
  };
  const start = () => enhance(document);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
  if (typeof MutationObserver !== "undefined") {
    const observer = new MutationObserver(records => {
      records.forEach(record => record.addedNodes.forEach(node => {
        if (node.nodeType === 1) enhance(node);
      }));
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
})();

/* Pointer-position ripple feedback for interactive controls. */
(() => {
  "use strict";
  const selector = "main .btn, main button, main input[type='submit'], main input[type='button'], main a.button, main .button-link, .actions a, .actions button";
  document.addEventListener("pointerdown", event => {
    if (event.button !== 0 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const control = event.target.closest(selector);
    if (!control || control.matches(":disabled, [aria-disabled='true']") || control.tagName === "INPUT") return;
    const rect = control.getBoundingClientRect();
    const ripple = document.createElement("span");
    ripple.className = "nexus-ripple";
    ripple.setAttribute("aria-hidden", "true");
    ripple.style.setProperty("--nexus-ripple-x", (event.clientX - rect.left) + "px");
    ripple.style.setProperty("--nexus-ripple-y", (event.clientY - rect.top) + "px");
    control.querySelectorAll(":scope > .nexus-ripple").forEach(item => item.remove());
    control.appendChild(ripple);
    ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
  }, { passive: true });
})();
