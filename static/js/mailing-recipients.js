(() => {
  const mode = document.getElementById("id_recipient_mode");
  const picker = document.getElementById("mailing-recipient-picker");
  if (!mode || !picker) return;

  const search = document.getElementById("mailing-recipient-search");
  const counter = document.getElementById("mailing-selection-count");
  const preview = document.getElementById("mailing-recipient-preview");
  const clear = document.getElementById("mailing-clear-selection");
  const options = Array.from(picker.querySelectorAll("[data-recipient-option]"));
  const checkboxes = Array.from(picker.querySelectorAll('input[type="checkbox"]'));
  const buttons = Array.from(document.querySelectorAll("[data-selected-label]"));
  buttons.forEach((button) => { button.dataset.allLabel = button.textContent; });

  const update = () => {
    const manual = mode.value === "selected";
    picker.hidden = !manual;
    if (preview) preview.hidden = manual;
    checkboxes.forEach((checkbox) => { checkbox.disabled = !manual; });
    const teachers = checkboxes.filter((box) => box.checked && box.name === "teacher_recipients").length;
    const organizers = checkboxes.filter((box) => box.checked && box.name === "organizer_recipients").length;
    counter.textContent = `${teachers} récapitulatif(s) de groupe et ${organizers} récapitulatif(s) de lieu sélectionnés. Les cases masquées par la recherche restent sélectionnées.`;
    buttons.forEach((button) => {
      button.textContent = manual ? button.dataset.selectedLabel : button.dataset.allLabel;
      const count = button.value === "send_groups" ? teachers : button.value === "send_organizers" ? organizers : teachers + organizers;
      button.disabled = manual && count === 0;
    });
  };

  const normalize = (value) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("fr");
  search.addEventListener("input", () => {
    const query = normalize(search.value.trim());
    options.forEach((option) => { option.hidden = !normalize(option.textContent).includes(query); });
  });
  mode.addEventListener("change", update);
  checkboxes.forEach((checkbox) => checkbox.addEventListener("change", update));
  clear.hidden = false;
  clear.addEventListener("click", () => {
    checkboxes.forEach((checkbox) => { checkbox.checked = false; });
    update();
  });
  update();
})();
