const root = document.querySelector("#dashboard-root");

if (root) {
  const endpoint = root.dataset.endpoint;
  const jiraConfigUrl = root.dataset.jiraConfigUrl;
  const jiraSaveUrl = root.dataset.jiraSaveUrl;
  const jiraTestUrl = root.dataset.jiraTestUrl;
  const jobFilter = document.querySelector("#job-filter");
  const sentimentFilter = document.querySelector("#sentiment-filter");
  const consequenceFilter = document.querySelector("#consequence-filter");
  const cardTotal = document.querySelector("#card-total");
  const cardNegative = document.querySelector("#card-negative");
  const cardJira = document.querySelector("#card-jira");
  const jobMeta = document.querySelector("#dashboard-job-meta");
  const issuesBody = document.querySelector("#critical-issues-body");
  const exportCsv = document.querySelector("#export-csv-btn");
  const exportDocx = document.querySelector("#export-docx-btn");
  const exportSelectedJira = document.querySelector("#export-selected-jira-btn");
  const selectAllFeedbacks = document.querySelector("#select-all-feedbacks");
  const deleteSelectedJob = document.querySelector("#delete-selected-job-btn");
  const exportStatus = document.querySelector("#jira-export-status");
  const exportStatusText = document.querySelector("#jira-export-status-text");
  const configureJira = document.querySelector("#configure-jira-btn");
  const jiraConfigModal = document.querySelector("#jira-config-modal");
  const closeJiraConfig = document.querySelector("#close-jira-config");
  const jiraConfigForm = document.querySelector("#jira-config-form");
  const testJiraConfig = document.querySelector("#test-jira-config");
  const jiraConfigMessage = document.querySelector("#jira-config-message");
  const jiraServer = document.querySelector("#jira-server");
  const jiraEmail = document.querySelector("#jira-email");
  const jiraToken = document.querySelector("#jira-token");
  const jiraProjectKey = document.querySelector("#jira-project-key");

  let consequenceChart = null;
  let criticalChart = null;
  let sentimentChart = null;
  let pollTimer = null;
  let currentExportJiraUrl = "";
  let isExportingJira = false;
  let jiraConfigured = false;

  function buildParams() {
    const params = new URLSearchParams();
    if (jobFilter?.value) {
      params.set("job", jobFilter.value);
    }
    params.set("sentiment", sentimentFilter?.value || "all");
    params.set("consequence", consequenceFilter?.value || "all");
    return params;
  }

  function setExportLinks(urls) {
    const csvUrl = urls?.csv || "#";
    const docxUrl = urls?.docx || "#";
    exportCsv.href = csvUrl;
    exportDocx.href = docxUrl;
    exportCsv.classList.toggle("disabled", csvUrl === "#");
    exportDocx.classList.toggle("disabled", docxUrl === "#");
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function selectedJobOption() {
    return jobFilter?.selectedOptions?.[0] || null;
  }

  function syncDeleteButton() {
    if (!deleteSelectedJob) {
      return;
    }
    const option = selectedJobOption();
    const isCompleted = option?.dataset.status === "completed";
    deleteSelectedJob.disabled = !isCompleted || !option?.dataset.deleteUrl;
  }

  function renderIssues(rows) {
    issuesBody.innerHTML = "";
    if (selectAllFeedbacks) {
      selectAllFeedbacks.checked = false;
      selectAllFeedbacks.disabled = true;
    }
    if (!rows?.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="8" class="text-muted">Nenhum feedback para os filtros atuais.</td>';
      issuesBody.appendChild(tr);
      syncExportSelectedButton();
      return;
    }

    for (const row of rows) {
      const isCreated = row.jira_url && row.jira_key && row.jira_key !== "-";
      const jiraCell = row.jira_url
        ? `<a href="${escapeHtml(row.jira_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.jira_key)}</a>`
        : escapeHtml(row.jira_status || row.jira_key || "pending");
      const feedbackText = escapeHtml(row.text);
      const selectionCell = isCreated
        ? '<span class="text-muted">-</span>'
        : `<input class="form-check-input feedback-export-check" type="checkbox" value="${escapeHtml(row.id)}" aria-label="Selecionar feedback ${escapeHtml(row.source_id)}">`;
      const actionCell = isCreated
        ? '<span class="text-muted">Exportado</span>'
        : `<button class="btn btn-sm btn-primary feedback-export-one" type="button" data-feedback-id="${escapeHtml(row.id)}">Exportar</button>`;

      const tr = document.createElement("tr");
      tr.dataset.feedbackId = row.id;
      tr.innerHTML = `
        <td class="critical-select">${selectionCell}</td>
        <td class="critical-id">${escapeHtml(row.source_id)}</td>
        <td class="critical-text-cell">
          <button class="critical-text-toggle" type="button" title="${feedbackText}" aria-expanded="false">
            <span>${feedbackText}</span>
          </button>
        </td>
        <td class="critical-sentiment">${escapeHtml(row.sentiment_score)}</td>
        <td class="critical-target">${escapeHtml(row.inferred_target)}</td>
        <td class="critical-consequence">${escapeHtml(row.consequence || "-")}</td>
        <td class="critical-jira">${jiraCell}</td>
        <td class="critical-action">${actionCell}</td>
      `;
      issuesBody.appendChild(tr);
    }
    syncExportSelectedButton();
  }

  function updateChart(chartRef, config) {
    if (!window.Chart) {
      return null;
    }
    if (!chartRef) {
      return new Chart(config.ctx, config.options);
    }

    chartRef.data.labels = config.options.data.labels;
    chartRef.data.datasets = config.options.data.datasets;
    chartRef.update();
    return chartRef;
  }

  function renderCharts(charts) {
    const consequenceLabels = charts?.consequence_distribution?.labels || [];
    const consequenceValues = charts?.consequence_distribution?.data || [];

    consequenceChart = updateChart(consequenceChart, {
      ctx: document.querySelector("#consequence-chart"),
      options: {
        type: "pie",
        data: {
          labels: consequenceLabels,
          datasets: [
            {
              data: consequenceValues,
              backgroundColor: ["#ca8a04", "#0f766e", "#0ea5e9"],
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: "bottom",
            },
          },
        },
      },
    });

    criticalChart = updateChart(criticalChart, {
      ctx: document.querySelector("#critical-chart"),
      options: {
        type: "bar",
        data: {
          labels: charts?.top_critical_features?.labels || [],
          datasets: [
            {
              label: "Corrections",
              data: charts?.top_critical_features?.data || [],
              backgroundColor: "#b42318",
              borderRadius: 6,
            },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: {
              beginAtZero: true,
            },
          },
          plugins: {
            legend: {
              display: false,
            },
          },
        },
      },
    });

    sentimentChart = updateChart(sentimentChart, {
      ctx: document.querySelector("#sentiment-chart"),
      options: {
        type: "bar",
        data: {
          labels: charts?.sentiment_by_category?.labels || [],
          datasets: [
            {
              label: "Sentimento medio",
              data: charts?.sentiment_by_category?.data || [],
              backgroundColor: "#1d4ed8",
              borderRadius: 6,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            y: {
              min: -1,
              max: 1,
            },
          },
          plugins: {
            legend: {
              display: false,
            },
          },
        },
      },
    });
  }

  function renderDashboard(payload) {
    cardTotal.textContent = payload?.cards?.total_feedbacks ?? 0;
    cardNegative.textContent = `${payload?.cards?.negative_percent ?? 0}%`;
    cardJira.textContent = payload?.cards?.jira_tickets ?? 0;

    if (payload?.job) {
      const job = payload.job;
      currentExportJiraUrl = job.export_jira_url || "";
      jobMeta.textContent = `Job #${job.id} | ${job.filename} | dominio: ${job.domain_name || "geral"} | ${job.processed_rows}/${job.total_rows} processados | status: ${job.status}`;
      const option = selectedJobOption();
      if (option) {
        option.dataset.status = job.status || option.dataset.status || "";
      }
    } else {
      currentExportJiraUrl = "";
      jobMeta.textContent = "Nenhum job disponivel.";
    }

    syncDeleteButton();
    renderCharts(payload?.charts || {});
    renderIssues(payload?.top_critical_issues || []);
    setExportLinks(payload?.export_urls || {});
  }

  function selectedFeedbackIds() {
    return Array.from(document.querySelectorAll(".feedback-export-check:checked")).map((input) => Number(input.value));
  }

  function syncExportSelectedButton() {
    const pendingChecks = Array.from(document.querySelectorAll(".feedback-export-check"));
    const selectedCount = pendingChecks.filter((input) => input.checked).length;
    if (exportSelectedJira) {
      if (isExportingJira) {
        exportSelectedJira.disabled = true;
        exportSelectedJira.textContent = "Exportando para o Jira...";
      } else {
      exportSelectedJira.disabled = isExportingJira || !jiraConfigured || !currentExportJiraUrl || selectedCount === 0;
      exportSelectedJira.textContent = selectedCount
        ? `Exportar ${selectedCount} Selecionado${selectedCount > 1 ? "s" : ""} para o Jira`
        : "Exportar Selecionados para o Jira";
      }
    }
    if (selectAllFeedbacks) {
      selectAllFeedbacks.disabled = pendingChecks.length === 0;
      selectAllFeedbacks.checked = pendingChecks.length > 0 && selectedCount === pendingChecks.length;
      selectAllFeedbacks.indeterminate = selectedCount > 0 && selectedCount < pendingChecks.length;
    }
  }

  function applyExportedRows(rows) {
    for (const row of rows || []) {
      const tr = issuesBody.querySelector(`tr[data-feedback-id="${String(row.id)}"]`);
      if (!tr) {
        continue;
      }
      const selectCell = tr.querySelector(".critical-select");
      const jiraCell = tr.querySelector(".critical-jira");
      const actionCell = tr.querySelector(".critical-action");
      if (selectCell) {
        selectCell.innerHTML = '<span class="text-muted">-</span>';
      }
      if (jiraCell) {
        jiraCell.innerHTML = row.jira_url
          ? `<a href="${escapeHtml(row.jira_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.jira_key)}</a>`
          : escapeHtml(row.jira_key || row.jira_status || "-");
      }
      if (actionCell) {
        actionCell.innerHTML = '<span class="text-muted">Exportado</span>';
      }
    }
    syncExportSelectedButton();
  }

  async function exportFeedbackIds(feedbackIds, button = null) {
    if (!currentExportJiraUrl || !feedbackIds.length) {
      return;
    }
    if (!jiraConfigured) {
      setExportStatus("error", "Configure os dados de integracao com o Jira antes de exportar.");
      openJiraConfigModal();
      return;
    }

    const previousText = button?.textContent || "";
    setExportBusy(true, button, "Exportando para o Jira...");
    setExportStatus("loading", `Enviando ${feedbackIds.length} feedback${feedbackIds.length > 1 ? "s" : ""} selecionado${feedbackIds.length > 1 ? "s" : ""} para o Jira...`, true);

    try {
      const response = await fetch(currentExportJiraUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken(),
        },
        body: JSON.stringify({feedbacks: feedbackIds}),
      });
      const data = await parseResponsePayload(response);
      if (!response.ok && !data.exported?.length) {
        throw new Error(data.error || data.errors?.[0]?.error || `Erro ${response.status}: Falha ao processar os dados selecionados.`);
      }
      applyExportedRows(data.exported || []);
      cardJira.textContent = data.jira_created ?? cardJira.textContent;
      if (data.errors?.length) {
        setExportStatus(
          "error",
          `${data.exported?.length || 0} exportados; ${data.errors.length} falharam. ${data.errors[0]?.error || ""}`.trim(),
        );
      } else {
        const count = data.exported?.length || 0;
        const dryRunCount = (data.exported || []).filter((row) => row.jira_status === "dry_run" || String(row.jira_key || "").startsWith("DRY-RUN-")).length;
        if (dryRunCount) {
          setExportStatus(
            "success",
            `${dryRunCount} feedback${dryRunCount === 1 ? "" : "s"} simulado${dryRunCount === 1 ? "" : "s"} em dry-run. Nenhuma tarefa foi criada no Jira; configure JIRA_DRY_RUN=false em producao.`,
          );
        } else {
          setExportStatus("success", `Sucesso! ${count} feedback${count === 1 ? "" : "s"} foram exportados e transformados em tarefas no Jira.`);
        }
      }
      await safeRefresh();
    } catch (error) {
      setExportStatus("error", error.message || "Erro ao exportar para o Jira.");
    } finally {
      setExportBusy(false, button, previousText || "Exportar");
    }
  }

  function setExportBusy(active, button = null, label = "") {
    isExportingJira = active;
    document.querySelectorAll(".feedback-export-check, .feedback-export-one").forEach((control) => {
      control.disabled = active;
    });
    if (selectAllFeedbacks) {
      selectAllFeedbacks.disabled = active || !document.querySelector(".feedback-export-check");
    }
    if (button) {
      button.disabled = active;
      if (label) {
        button.textContent = label;
      }
    }
    if (exportSelectedJira && button !== exportSelectedJira && active) {
      exportSelectedJira.disabled = true;
    }
    if (!active && button) {
      button.textContent = label;
    }
    syncExportSelectedButton();
  }

  function setExportStatus(type, message, loading = false) {
    if (!exportStatus || !exportStatusText) {
      showError(message);
      return;
    }
    exportStatus.hidden = false;
    exportStatus.dataset.status = type;
    exportStatus.classList.toggle("is-loading", Boolean(loading));
    exportStatusText.textContent = message;
    if (!loading) {
      exportStatus.scrollIntoView({behavior: "smooth", block: "nearest"});
    }
  }

  async function parseResponsePayload(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return response.json();
    }
    const text = await response.text();
    return {
      error: `Erro ${response.status}: ${text ? text.slice(0, 240) : "Falha ao processar os dados selecionados."}`,
      exported: [],
      errors: [],
    };
  }

  async function loadJiraConfig() {
    if (!jiraConfigUrl) {
      return;
    }
    try {
      const response = await fetch(jiraConfigUrl);
      const data = await response.json();
      jiraConfigured = Boolean(data.configured);
      if (configureJira) {
        configureJira.textContent = jiraConfigured ? "Jira configurado" : "Configurar Jira";
        configureJira.classList.toggle("btn-outline-primary", jiraConfigured);
        configureJira.classList.toggle("btn-warning", !jiraConfigured);
      }
      const source = data.configured ? data : data.defaults || {};
      if (jiraServer) jiraServer.value = source.server || "";
      if (jiraEmail) jiraEmail.value = source.email || "";
      if (jiraProjectKey) jiraProjectKey.value = source.project_key || "";
      if (jiraToken) jiraToken.value = "";
      syncExportSelectedButton();
    } catch (error) {
      jiraConfigured = false;
      setExportStatus("error", "Nao foi possivel verificar a configuracao Jira.");
    }
  }

  function openJiraConfigModal() {
    if (!jiraConfigModal) {
      return;
    }
    jiraConfigModal.hidden = false;
    jiraServer?.focus();
  }

  function closeJiraConfigModal() {
    if (jiraConfigModal) {
      jiraConfigModal.hidden = true;
    }
  }

  function jiraFormPayload() {
    return {
      server: jiraServer?.value || "",
      email: jiraEmail?.value || "",
      api_token: jiraToken?.value || "",
      project_key: jiraProjectKey?.value || "",
    };
  }

  function setJiraConfigMessage(type, message) {
    if (!jiraConfigMessage) {
      return;
    }
    jiraConfigMessage.hidden = false;
    jiraConfigMessage.dataset.status = type;
    jiraConfigMessage.textContent = message;
  }

  async function submitJiraConfig(url, successMessage, button = null) {
    const previousText = button?.textContent || "";
    if (button) {
      button.disabled = true;
      button.textContent = "Aguarde...";
    }
    setJiraConfigMessage("loading", "Validando dados de integracao...");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken(),
        },
        body: JSON.stringify(jiraFormPayload()),
      });
      const data = await parseResponsePayload(response);
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `Erro ${response.status}: nao foi possivel comunicar com o Jira.`);
      }
      setJiraConfigMessage("success", data.message || successMessage);
      return data;
    } catch (error) {
      setJiraConfigMessage("error", error.message || "Erro ao configurar Jira.");
      return null;
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = previousText;
      }
    }
  }

  function syncQueryString(params) {
    const url = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, "", url);
  }

  async function loadDashboard() {
    const params = buildParams();
    syncQueryString(params);

    if (!jobFilter?.value) {
      renderDashboard({
        job: null,
        cards: {total_feedbacks: 0, negative_percent: 0, jira_tickets: 0},
        charts: {
          consequence_distribution: {labels: [], data: []},
          top_critical_features: {labels: [], data: []},
          sentiment_by_category: {labels: [], data: []},
        },
        top_critical_issues: [],
        export_urls: {csv: "#", docx: "#"},
      });
      return;
    }

    const response = await fetch(`${endpoint}?${params.toString()}`);
    if (!response.ok) {
      throw new Error("Nao foi possivel carregar os dados do dashboard.");
    }

    const payload = await response.json();
    renderDashboard(payload);
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function showError(message) {
    jobMeta.textContent = message;
  }

  async function safeRefresh() {
    try {
      await loadDashboard();
    } catch (error) {
      showError(error.message || "Erro ao atualizar dashboard.");
    }
  }

  [jobFilter, sentimentFilter, consequenceFilter].forEach((el) => {
    el?.addEventListener("change", () => {
      syncDeleteButton();
      safeRefresh();
    });
  });

  configureJira?.addEventListener("click", openJiraConfigModal);
  closeJiraConfig?.addEventListener("click", closeJiraConfigModal);
  jiraConfigModal?.addEventListener("click", (event) => {
    if (event.target === jiraConfigModal) {
      closeJiraConfigModal();
    }
  });

  testJiraConfig?.addEventListener("click", async () => {
    const data = await submitJiraConfig(jiraTestUrl, "Comunicacao com o Jira realizada com sucesso.", testJiraConfig);
    if (data?.ok) {
      const types = Array.isArray(data.issue_types) && data.issue_types.length
        ? ` Tipos disponiveis: ${data.issue_types.join(", ")}.`
        : "";
      setJiraConfigMessage("success", `${data.message}${types}`);
    }
  });

  jiraConfigForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = jiraConfigForm.querySelector('button[type="submit"]');
    const data = await submitJiraConfig(jiraSaveUrl, "Configuracao Jira salva.", submitButton);
    if (data?.configured) {
      jiraConfigured = true;
      configureJira.textContent = "Jira configurado";
      configureJira.classList.add("btn-outline-primary");
      configureJira.classList.remove("btn-warning");
      syncExportSelectedButton();
      setExportStatus("success", "Configuracao Jira salva. Agora voce pode exportar feedbacks selecionados.");
      closeJiraConfigModal();
    }
  });

  deleteSelectedJob?.addEventListener("click", async () => {
    const option = selectedJobOption();
    const deleteUrl = option?.dataset.deleteUrl;
    const label = option?.textContent?.trim() || "este job";
    if (!deleteUrl || deleteSelectedJob.disabled) {
      return;
    }
    if (!window.confirm(`Deletar ${label}? Esta acao remove os feedbacks e eventos analisados.`)) {
      return;
    }

    deleteSelectedJob.disabled = true;
    deleteSelectedJob.textContent = "Deletando...";
    try {
      const response = await fetch(deleteUrl, {
        method: "POST",
        headers: {"X-CSRFToken": csrfToken()},
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "Nao foi possivel deletar o job.");
      }
      option.remove();
      if (jobFilter.options.length) {
        jobFilter.selectedIndex = 0;
      }
      jobMeta.textContent = data.message || "Job deletado.";
      await safeRefresh();
    } catch (error) {
      showError(error.message || "Erro ao deletar job.");
    } finally {
      deleteSelectedJob.textContent = "Deletar job";
      syncDeleteButton();
    }
  });

  issuesBody?.addEventListener("click", (event) => {
    const exportButton = event.target.closest(".feedback-export-one");
    if (exportButton) {
      exportFeedbackIds([Number(exportButton.dataset.feedbackId)], exportButton);
      return;
    }

    const toggle = event.target.closest(".critical-text-toggle");
    if (!toggle) {
      return;
    }
    const expanded = toggle.classList.toggle("is-expanded");
    toggle.setAttribute("aria-expanded", String(expanded));
  });

  issuesBody?.addEventListener("change", (event) => {
    if (event.target.classList.contains("feedback-export-check")) {
      syncExportSelectedButton();
    }
  });

  selectAllFeedbacks?.addEventListener("change", () => {
    document.querySelectorAll(".feedback-export-check").forEach((input) => {
      input.checked = selectAllFeedbacks.checked;
    });
    syncExportSelectedButton();
  });

  exportSelectedJira?.addEventListener("click", async () => {
    const feedbackIds = selectedFeedbackIds();
    if (!feedbackIds.length) {
      syncExportSelectedButton();
      return;
    }
    await exportFeedbackIds(feedbackIds, exportSelectedJira);
  });

  syncDeleteButton();
  loadJiraConfig();
  safeRefresh();
  pollTimer = setInterval(safeRefresh, 5000);

  window.addEventListener("beforeunload", () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  });
}
