(() => {
  const updateAllocation = (button, useFullGroup) => {
    const studentInput = document.getElementById(button.dataset.studentTarget);
    const chaperoneInput = document.getElementById(button.dataset.chaperoneTarget);
    if (!studentInput || !chaperoneInput) return;

    if (!studentInput.disabled) {
      studentInput.value = useFullGroup ? button.dataset.studentCount : "0";
      studentInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
    if (!chaperoneInput.disabled) {
      chaperoneInput.value = useFullGroup ? button.dataset.chaperoneCount : "0";
      chaperoneInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
  };

  document.querySelectorAll("[data-fill-full-group]").forEach((button) => {
    button.addEventListener("click", () => updateAllocation(button, true));
  });
  document.querySelectorAll("[data-clear-group]").forEach((button) => {
    button.addEventListener("click", () => updateAllocation(button, false));
  });
})();
