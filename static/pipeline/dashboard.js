const root = document.querySelector("#dashboard-root");

if (root) {
  const endpoint = root.dataset.endpoint;
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

  let consequenceChart = null;
  let criticalChart = null;
  let sentimentChart = null;
  let pollTimer = null;

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

  function renderIssues(rows) {
    issuesBody.innerHTML = "";
    if (!rows?.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="5" class="text-muted">Nenhum issue critico para os filtros atuais.</td>';
      issuesBody.appendChild(tr);
      return;
    }

    for (const row of rows) {
      const jiraCell = row.jira_url
        ? `<a href="${escapeHtml(row.jira_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.jira_key)}</a>`
        : escapeHtml(row.jira_key || "-");

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(row.source_id)}</td>
        <td>${escapeHtml(row.text)}</td>
        <td>${escapeHtml(row.sentiment_score)}</td>
        <td>${escapeHtml(row.inferred_target)}</td>
        <td>${jiraCell}</td>
      `;
      issuesBody.appendChild(tr);
    }
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
      jobMeta.textContent = `Job #${job.id} | ${job.filename} | ${job.processed_rows}/${job.total_rows} processados | status: ${job.status}`;
    } else {
      jobMeta.textContent = "Nenhum job disponivel.";
    }

    renderCharts(payload?.charts || {});
    renderIssues(payload?.top_critical_issues || []);
    setExportLinks(payload?.export_urls || {});
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
      safeRefresh();
    });
  });

  safeRefresh();
  pollTimer = setInterval(safeRefresh, 5000);

  window.addEventListener("beforeunload", () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  });
}
