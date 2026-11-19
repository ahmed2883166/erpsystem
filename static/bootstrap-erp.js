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


/* Searchable native selects: retain the original select as the sole form value.
   Long lists (accounts, customers, suppliers, products, dimensions) get an
   accessible filter field without changing names, option values or submission. */
(() => {
  "use strict";
  const MIN_OPTIONS = 12;
  let searchId = 0;
  const normalize = value => String(value || "")
    .normalize("NFD")
    .replace(/[\\u0300-\\u036f]/g, "")
    .replace(/[أإآ]/g, "ا")
    .replace(/ى/g, "ي")
    .replace(/ة/g, "ه")
    .toLocaleLowerCase("ar")
    .trim();

  const searchable = select => {
    if (select.multiple || select.dataset.nexusSearchReady === "1") return;
    const options = [...select.options];
    if (options.length < MIN_OPTIONS) return;
    if (select.closest(".no-search-select,[data-no-search]")) return;
    if (select.disabled) return;

    select.dataset.nexusSearchReady = "1";
    const wrapper = document.createElement("div");
    wrapper.className = "nexus-search-select";
    const search = document.createElement("input");
    search.type = "search";
    search.className = "nexus-select-search";
    search.autocomplete = "off";
    search.placeholder = "ابحث بالكود أو الاسم...";
    search.setAttribute("aria-label", "بحث داخل القائمة");
    search.id = "nexus-select-search-" + (++searchId);
    search.setAttribute("aria-controls", select.id || search.id);
    search.setAttribute("dir", "auto");
    const status = document.createElement("span");
    status.className = "nexus-select-search-count";
    status.setAttribute("aria-live", "polite");

    select.parentNode.insertBefore(wrapper, select);
    wrapper.append(search, status, select);
    select.classList.add("nexus-searchable-native");

    const refresh = () => {
      const query = normalize(search.value);
      let visible = 0;
      const groups = new Map();
      [...select.children].forEach(child => {
        if (child.tagName === "OPTGROUP") {
          const groupOptions = [...child.children];
          const groupMatches = normalize(child.label).includes(query);
          let groupVisible = 0;
          groupOptions.forEach(option => {
            const match = !query || option.selected || groupMatches || normalize(option.textContent).includes(query);
            option.hidden = !match;
            if (match) { visible++; groupVisible++; }
          });
          child.hidden = !!query && groupVisible === 0;
        } else if (child.tagName === "OPTION") {
          const match = !query || child.selected || normalize(child.textContent).includes(query);
          child.hidden = !match;
          if (match) visible++;
        }
      });
      status.textContent = query ? (visible ? visible + " نتيجة" : "لا توجد نتائج") : "";
    };

    search.addEventListener("input", refresh);
    search.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        select.focus();
        select.click();
      } else if (event.key === "Escape") {
        search.value = "";
        refresh();
        select.focus();
      }
    });
    select.addEventListener("change", () => {
      // Keep the filter visible while the user makes successive selections.
      select.dispatchEvent(new Event("nexus:search-selection", { bubbles: true }));
    });
    refresh();

    // Options may be appended later by invoice-line and dialog scripts.
    if (typeof MutationObserver !== "undefined") {
      const observer = new MutationObserver(refresh);
      observer.observe(select, { childList: true, subtree: true, characterData: true });
    }
  };

  const scan = root => {
    if (!root || !root.querySelectorAll) return;
    if (root.matches?.("main select")) searchable(root);
    root.querySelectorAll("main select").forEach(searchable);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => scan(document), { once: true });
  } else scan(document);

  if (typeof MutationObserver !== "undefined") {
    const observer = new MutationObserver(records => {
      records.forEach(record => record.addedNodes.forEach(node => {
        if (node.nodeType === 1) scan(node);
      }));
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
})();
