(() => {
  const summary = document.querySelector("[data-planning-summary]");
  if (!summary) return;

  const form = summary.closest("form");
  const items = Array.from(
    summary.querySelectorAll("[data-planning-summary-item]"),
  );
  const emptyState = summary.querySelector("[data-planning-summary-empty]");
  const selectionCount = summary.querySelector("[data-planning-summary-count]");
  const toggle = summary.querySelector("[data-planning-summary-toggle]");
  const storageKey = `ltnm-auto-filter:${window.location.pathname}:draft`;
  const collapsedStorageKey = `${storageKey}:summary-collapsed`;

  const setCollapsed = (collapsed) => {
    summary.classList.toggle("is-collapsed", collapsed);
    if (toggle) {
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.textContent = collapsed ? "Afficher" : "Réduire";
    }
  };

  const storedValues = () => {
    try {
      const draft = JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
      return draft?.values || {};
    } catch (_error) {
      return {};
    }
  };

  const countFor = (fieldName, fallback, savedValues) => {
    const field = form?.elements.namedItem(fieldName);
    const value = field?.value ?? savedValues[fieldName] ?? fallback ?? "0";
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  };

  const plural = (count, singular, pluralLabel) =>
    `${count} ${count === 1 ? singular : pluralLabel}`;

  const refresh = () => {
    const savedValues = storedValues();
    let selected = 0;
    items.forEach((item) => {
      const students = countFor(
        item.dataset.studentField,
        item.dataset.studentCount,
        savedValues,
      );
      const chaperones = countFor(
        item.dataset.chaperoneField,
        item.dataset.chaperoneCount,
        savedValues,
      );
      const isSelected = students > 0;
      item.hidden = !isSelected;
      if (!isSelected) return;

      selected += 1;
      const participants = item.querySelector(
        "[data-planning-summary-participants]",
      );
      if (participants) {
        participants.textContent = `${plural(students, "élève", "élèves")} + ${plural(
          chaperones,
          "accompagnateur",
          "accompagnateurs",
        )}`;
      }
    });
    if (selectionCount) selectionCount.textContent = String(selected);
    if (emptyState) emptyState.hidden = selected > 0;
  };

  form?.addEventListener("input", refresh);
  form?.addEventListener("change", refresh);
  toggle?.addEventListener("click", () => {
    const collapsed = !summary.classList.contains("is-collapsed");
    setCollapsed(collapsed);
    try {
      window.sessionStorage.setItem(collapsedStorageKey, collapsed ? "1" : "0");
    } catch (_error) {
      // The panel still remains collapsible when storage is unavailable.
    }
  });
  window.addEventListener("pageshow", refresh);
  try {
    setCollapsed(window.sessionStorage.getItem(collapsedStorageKey) === "1");
  } catch (_error) {
    setCollapsed(false);
  }
  refresh();
})();
