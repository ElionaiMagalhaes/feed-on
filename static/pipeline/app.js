const form = document.querySelector("#upload-form");
const datasetInput = document.querySelector("#dataset");
const dropzone = document.querySelector("#dropzone");
const currentPhase = document.querySelector("#current-phase");
const jobStatus = document.querySelector("#job-status");
const progressBar = document.querySelector("#progress-bar");
const eventList = document.querySelector("#event-list");
const resultBody = document.querySelector("#result-body");
const rowCount = document.querySelector("#row-count");
const cancelButton = document.querySelector("#cancel-job");
const jobDetail = document.querySelector("#job-detail");
const selectedFile = document.querySelector("#selected-file");
const fileFeedback = document.querySelector("#file-feedback");
const fileFeedbackName = document.querySelector("#file-feedback-name");
const fileFeedbackMeta = document.querySelector("#file-feedback-meta");

let pollTimer = null;
let activeCancelUrl = "";

function setSelectedFile(file) {
  if (!file) {
    return;
  }
  selectedFile.textContent = file.name;
  fileFeedback.hidden = false;
  fileFeedback.className = "file-feedback is-ready";
  fileFeedbackName.textContent = file.name;
  fileFeedbackMeta.textContent = `${formatFileSize(file.size)} selecionado. Pronto para iniciar.`;
}

function setFileFeedbackState(state, message = "") {
  if (!fileFeedback || fileFeedback.hidden) {
    return;
  }
  fileFeedback.className = `file-feedback is-${state}`;
  if (state === "uploading") {
    fileFeedbackMeta.textContent = "Enviando arquivo...";
  } else if (state === "accepted") {
    fileFeedbackMeta.textContent = "Arquivo recebido. Processamento iniciado.";
  } else if (state === "error") {
    fileFeedbackMeta.textContent = message || "Falha no upload.";
  }
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes)) {
    return "Tamanho desconhecido";
  }
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(size >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}
function csrfToken() {
  const input = form.querySelector("input[name=csrfmiddlewaretoken]");
  return input ? input.value : "";
}

function setStatus(data) {
  currentPhase.textContent = data.current_phase || "Processando...";
  jobStatus.textContent = data.status || "running";
  progressBar.style.width = `${data.progress_percent || 0}%`;
  progressBar.textContent = `${data.progress_percent || 0}%`;
  activeCancelUrl = data.cancel_url || activeCancelUrl;
  eventList.innerHTML = "";

  for (const event of data.events || []) {
    const item = document.createElement("li");
    item.dataset.level = event.level;
    item.textContent = event.message;
    eventList.appendChild(item);
  }

  const total = data.total_rows || 0;
  const processed = data.processed_rows || 0;
  const limit = data.row_limit ? ` Limite: ${data.row_limit}.` : "";
  jobDetail.textContent = total ? `${processed}/${total} feedbacks.${limit}` : "Preparando arquivo...";
  cancelButton.disabled = !isCancelable(data.status) || data.cancel_requested;
  cancelButton.textContent = data.cancel_requested ? "Cancelando..." : "Cancelar processamento";

  renderRows(data.feedbacks || []);

  if (["completed", "failed", "canceled"].includes(data.status)) {
    clearInterval(pollTimer);
    pollTimer = null;
    cancelButton.disabled = true;
  }
}

function isCancelable(status) {
  return ["pending", "running", "canceling"].includes(status);
}

function renderRows(rows) {
  rowCount.textContent = `${rows.length} feedbacks`;
  resultBody.innerHTML = "";

  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td colspan="8" class="text-muted">Aguardando os primeiros registros processados.</td>';
    resultBody.appendChild(tr);
    return;
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.source_id)}</td>
      <td>${escapeHtml(row.text)}</td>
      <td>${escapeHtml(row.intent || "-")}</td>
      <td>${escapeHtml(formatSentiment(row.sentiment_score, row.ai_intent, row.ai_provider))}</td>
      <td>${escapeHtml(row.target_candidate || "-")}</td>
      <td>${escapeHtml(row.inferred_target || row.technical_target || "-")}</td>
      <td>${escapeHtml(row.consequence || "-")}</td>
      <td><strong>${escapeHtml(row.jira_key || row.jira_status || "-")}</strong></td>
    `;
    resultBody.appendChild(tr);
  }
}

function poll(statusUrl) {
  clearInterval(pollTimer);
  const run = async () => {
    const response = await fetch(statusUrl);
    if (!response.ok) {
      throw new Error("Nao foi possivel consultar o status.");
    }
    setStatus(await response.json());
  };
  run().catch(showError);
  pollTimer = setInterval(() => run().catch(showError), 1500);
}

async function cancelActiveJob() {
  if (!activeCancelUrl || cancelButton.disabled) {
    return;
  }
  cancelButton.disabled = true;
  cancelButton.textContent = "Cancelando...";
  try {
    const response = await fetch(activeCancelUrl, {
      method: "POST",
      headers: {"X-CSRFToken": csrfToken()},
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Nao foi possivel cancelar.");
    }
    currentPhase.textContent = data.message || "Cancelamento solicitado.";
  } catch (error) {
    setFileFeedbackState("error", error.message);
    showError(error);
  }
}

function showError(error) {
  currentPhase.textContent = error.message || "Erro inesperado";
  jobStatus.textContent = "error";
}

function formatSentiment(score, intent, provider) {
  const value = score === null || score === undefined ? "-" : Number(score).toFixed(2);
  const source = provider ? ` via ${provider}` : "";
  const label = intent ? ` ${intent}` : "";
  return `${value}${label}${source}`;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!datasetInput.files.length) {
    showError(new Error("Selecione um CSV antes de iniciar."));
    return;
  }

  currentPhase.textContent = "Enviando arquivo...";
  jobStatus.textContent = "uploading";
  jobDetail.textContent = "Upload em andamento.";
  setFileFeedbackState("uploading");
  cancelButton.disabled = true;
  const body = new FormData(form);

  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: {"X-CSRFToken": csrfToken()},
      body,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Falha no upload.");
    }
    activeCancelUrl = data.cancel_url || "";
    setFileFeedbackState("accepted");
    poll(data.status_url);
  } catch (error) {
    setFileFeedbackState("error", error.message);
    showError(error);
  }
});

cancelButton.addEventListener("click", cancelActiveJob);

datasetInput.addEventListener("change", () => {
  if (datasetInput.files.length) {
    setSelectedFile(datasetInput.files[0]);
  }
});

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("is-dragging");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("is-dragging");
});

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("is-dragging");
  if (event.dataTransfer.files.length) {
    datasetInput.files = event.dataTransfer.files;
    setSelectedFile(event.dataTransfer.files[0]);
  }
});

document.querySelectorAll(".recent-job").forEach((button) => {
  button.addEventListener("click", () => poll(button.dataset.statusUrl));
});



