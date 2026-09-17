(() => {
  "use strict";
  const form = document.getElementById("bulk-confirmation-run");
  if (!form) return;
  const rows = Array.from(document.querySelectorAll("[data-bulk-reference]"));
  const progress = document.getElementById("bulk-progress");
  const label = document.getElementById("bulk-progress-label");
  const error = document.getElementById("bulk-error");
  const resume = document.getElementById("bulk-resume");
  let running = false;
  let completed = 0;
  let sent = 0;
  let failed = 0;
  let skipped = 0;

  function updateProgress() {
    progress.value = completed;
    label.textContent = `${completed} / ${rows.length} groupes traités — ` +
      `${sent} validés avec mail envoyé, ${failed} validés avec échec d’envoi, ` +
      `${skipped} à vérifier.`;
  }

  async function run() {
    if (running) return;
    running = true;
    resume.hidden = true;
    error.hidden = true;
    try {
      for (; completed < rows.length;) {
        const row = rows[completed];
        const status = row.querySelector("[data-bulk-status]");
        status.textContent = "Validation et envoi en cours…";
        const body = new FormData(form);
        body.set("reference", row.dataset.bulkReference);
        const response = await fetch(form.action, {
          method: "POST", body, credentials: "same-origin",
          headers: {"Accept": "application/json"},
        });
        if (response.redirected) {
          throw new Error("Votre session a expiré. Reconnectez-vous dans un autre onglet, puis reprenez.");
        }
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Envoi interrompu.");
        if (!["sent", "failed", "skipped"].includes(result.status)) {
          throw new Error("Réponse inattendue du serveur.");
        }
        status.textContent = result.message;
        if (result.status === "sent") sent++;
        else if (result.status === "failed") failed++;
        else skipped++;
        completed++;
        updateProgress();
      }
      label.textContent = "Traitement terminé. " + label.textContent;
    } catch (exception) {
      error.textContent = "Traitement interrompu : " + exception.message +
        " Vous pouvez reprendre sans renvoyer les mails des groupes déjà validés. " +
        "Vérifiez la fiche du groupe interrompu si son résultat reste incertain.";
      error.hidden = false;
      resume.hidden = false;
    } finally {
      running = false;
    }
  }
  window.addEventListener("beforeunload", (event) => {
    if (running) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  form.addEventListener("submit", (event) => event.preventDefault());
  resume.addEventListener("click", run);
  run();
})();
