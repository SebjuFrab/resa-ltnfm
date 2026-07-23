(() => {
  const storagePrefix = `ltnm-auto-filter:${window.location.pathname}`;
  const focusStorageKey = `${storagePrefix}:search-focus`;
  const scrollStorageKey = `${storagePrefix}:scroll`;
  const draftStorageKey = `${storagePrefix}:draft`;
  const draftMaxAge = 30 * 60 * 1000;
  const planningForm = document.querySelector("form[data-filter-draft]");

  if ("scrollRestoration" in window.history) {
    window.history.scrollRestoration = "manual";
  }

  const clearFilterDraft = () => {
    try {
      window.sessionStorage.removeItem(draftStorageKey);
    } catch (_error) {
      // Ignore unavailable storage.
    }
  };

  const readStoredDraft = () => {
    try {
      const storedDraft = JSON.parse(
        window.sessionStorage.getItem(draftStorageKey) || "null",
      );
      if (
        storedDraft &&
        Date.now() - storedDraft.savedAt <= draftMaxAge &&
        storedDraft.values
      ) {
        return storedDraft;
      }
    } catch (_error) {
      // Invalid or unavailable storage is treated like an absent draft.
    }
    clearFilterDraft();
    return null;
  };

  let scrollTop = null;
  try {
    const storedScroll = window.sessionStorage.getItem(scrollStorageKey);
    if (storedScroll !== null) {
      window.sessionStorage.removeItem(scrollStorageKey);
      const parsedScroll = Number(storedScroll);
      if (Number.isFinite(parsedScroll) && parsedScroll >= 0) scrollTop = parsedScroll;
    }
  } catch (_error) {
    // Storage may be unavailable; the page remains usable without restoration.
  }

  if (planningForm) {
    const storedDraft = readStoredDraft();
    if (storedDraft) {
      planningForm
        .querySelectorAll(".reservation-count[name]")
        .forEach((input) => {
          if (
            Object.prototype.hasOwnProperty.call(storedDraft.values, input.name)
          ) {
            input.value = storedDraft.values[input.name];
          }
        });
    }
  }

  if (scrollTop !== null) {
    const root = document.documentElement;
    const previousScrollBehavior = root.style.scrollBehavior;
    const restoreScroll = () => window.scrollTo(0, scrollTop);

    root.style.scrollBehavior = "auto";
    restoreScroll();
    window.requestAnimationFrame(() => {
      restoreScroll();
      window.requestAnimationFrame(() => {
        root.style.scrollBehavior = previousScrollBehavior;
      });
    });
  }

  const rememberFilterState = () => {
    try {
      window.sessionStorage.setItem(scrollStorageKey, String(window.scrollY));
      if (!planningForm) return;

      let values = {};
      const storedDraft = readStoredDraft();
      if (storedDraft) values = storedDraft.values;
      planningForm
        .querySelectorAll(".reservation-count[name]")
        .forEach((input) => {
          values[input.name] = input.value;
        });
      window.sessionStorage.setItem(
        draftStorageKey,
        JSON.stringify({ savedAt: Date.now(), values }),
      );
    } catch (_error) {
      // Filtering still works when session storage is unavailable.
    }
  };

  if (planningForm) {
    planningForm.addEventListener("submit", () => {
      try {
        const storedDraft = readStoredDraft();
        const existingNames = new Set(
          Array.from(planningForm.elements, (field) => field.name).filter(Boolean),
        );
        if (storedDraft?.values) {
          Object.entries(storedDraft.values).forEach(([name, value]) => {
            if (existingNames.has(name)) return;
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            input.value = value;
            planningForm.appendChild(input);
          });
        }
      } catch (_error) {
        // Visible values and saved server values remain authoritative.
      }
      if (planningForm.dataset.unfilteredAction) {
        planningForm.action = planningForm.dataset.unfilteredAction;
      }
      clearFilterDraft();
    });
    document.addEventListener("click", (event) => {
      const link = event.target.closest("a[href]");
      if (link && !link.closest("form[data-auto-submit-filters]")) {
        clearFilterDraft();
      }
    });
  }

  document.querySelectorAll("form[data-auto-submit-filters]").forEach((form) => {
    const searchInput = form.querySelector('input[name="q"]');
    let searchTimer;

    const submitFilters = (restoreSearchFocus = false) => {
      window.clearTimeout(searchTimer);
      if (restoreSearchFocus) {
        try {
          window.sessionStorage.setItem(focusStorageKey, "1");
        } catch (_error) {
          // Storage may be unavailable; filtering still works without focus restoration.
        }
      }
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        rememberFilterState();
        form.submit();
      }
    };

    form.addEventListener("change", (event) => {
      if (event.target !== searchInput) submitFilters();
    });

    form.querySelectorAll("[data-clear-filters]").forEach((link) => {
      link.addEventListener("click", rememberFilterState);
    });

    if (searchInput) {
      try {
        if (window.sessionStorage.getItem(focusStorageKey) === "1") {
          window.sessionStorage.removeItem(focusStorageKey);
          searchInput.focus({ preventScroll: true });
          searchInput.setSelectionRange(
            searchInput.value.length,
            searchInput.value.length,
          );
        }
      } catch (_error) {
        // Ignore unavailable storage.
      }
      searchInput.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => submitFilters(true), 500);
      });
      searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          submitFilters(true);
        }
      });
    }

    form.addEventListener("submit", () => {
      window.clearTimeout(searchTimer);
      rememberFilterState();
    });
  });
})();
