const state = {
  timer: null,
  data: null,
};

const keyInput = document.querySelector("#api-key");
const windowSelect = document.querySelector("#window-hours");
const saveKeyButton = document.querySelector("#save-key");
const refreshButton = document.querySelector("#refresh");

const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
const hashKey = hashParams.get("api_key") || hashParams.get("key") || "";
if (hashKey) {
  sessionStorage.setItem("centaur.ops.apiKey", hashKey.trim());
  sessionStorage.setItem("centaur.ops.autoAuth", "1");
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
}

const savedKey = sessionStorage.getItem("centaur.ops.apiKey") || "";
const autoAuth = sessionStorage.getItem("centaur.ops.autoAuth") === "1";
if (autoAuth) {
  keyInput.closest(".field").hidden = true;
  saveKeyButton.hidden = true;
} else if (savedKey) {
  keyInput.value = savedKey;
}

saveKeyButton.addEventListener("click", () => {
  sessionStorage.removeItem("centaur.ops.autoAuth");
  sessionStorage.setItem("centaur.ops.apiKey", keyInput.value.trim());
  loadSummary();
});

refreshButton.addEventListener("click", () => loadSummary());
windowSelect.addEventListener("change", () => loadSummary());

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString();
}

function fmtAge(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
  return `${(value / 86400).toFixed(1)}d`;
}

function statusLabel(status) {
  const normalized = String(status || "ok");
  return `<span class="badge ${escapeHtml(normalized)}">${escapeHtml(normalized)}</span>`;
}

function setText(selector, value) {
  const node = document.querySelector(selector);
  if (node) node.textContent = value;
}

function renderMonitors(monitors) {
  const root = document.querySelector("#monitors");
  if (!monitors?.length) {
    root.innerHTML = '<p class="empty">No monitors reported.</p>';
    return;
  }
  root.innerHTML = monitors
    .map(
      (monitor) => `
        <article class="monitor ${escapeHtml(monitor.status)}">
          <div class="monitor-title">
            <strong>${escapeHtml(monitor.title)}</strong>
            ${statusLabel(monitor.status)}
          </div>
          <p>${escapeHtml(monitor.summary)}</p>
          <p class="muted">${escapeHtml(monitor.id)}</p>
        </article>
      `,
    )
    .join("");
}

function renderErrors(errors) {
  const root = document.querySelector("#errors");
  if (!errors?.length) {
    root.innerHTML = '<p class="empty">No recent errors.</p>';
    return;
  }
  root.innerHTML = errors
    .map(
      (item) => `
        <article class="feed-item">
          <div class="feed-title">
            <strong>${escapeHtml(item.title)}</strong>
            ${statusLabel(item.severity)}
          </div>
          <p>${escapeHtml(item.message)}</p>
          <p class="feed-meta">
            ${escapeHtml(item.component)} · ${escapeHtml(item.status)} ·
            ${escapeHtml(item.thread_key || item.id)} · ${fmtTime(item.updated_at)}
          </p>
        </article>
      `,
    )
    .join("");
}

function workItem(title, item, extra) {
  return `
    <article class="feed-item">
      <div class="feed-title">
        <strong>${escapeHtml(title)}</strong>
        ${statusLabel("warning")}
      </div>
      <p>${escapeHtml(extra)}</p>
      <p class="feed-meta">
        ${escapeHtml(item.workflow_name || item.execution_id || item.schedule_id || item.thread_key)}
        ${item.thread_key ? ` · ${escapeHtml(item.thread_key)}` : ""}
      </p>
    </article>
  `;
}

function renderStuckWork(stuck) {
  const root = document.querySelector("#stuck-work");
  if (!stuck) {
    root.innerHTML = '<p class="empty">No stuck work data.</p>';
    return;
  }
  const items = [
    ...(stuck.running_workflows || []).map((item) =>
      workItem("Running workflow", item, `Age ${fmtAge(item.age_seconds)}`),
    ),
    ...(stuck.overdue_workflows || []).map((item) =>
      workItem("Overdue workflow", item, `Overdue ${fmtAge(item.overdue_seconds)}`),
    ),
    ...(stuck.queued_executions || []).map((item) =>
      workItem("Queued execution", item, `Age ${fmtAge(item.age_seconds)}`),
    ),
    ...(stuck.running_executions || []).map((item) =>
      workItem("Running execution", item, `Age ${fmtAge(item.age_seconds)}`),
    ),
    ...(stuck.deliveries || []).map((item) =>
      workItem("Slack delivery", item, `Overdue ${fmtAge(item.overdue_seconds)}`),
    ),
    ...(stuck.schedule_lag || []).map((item) =>
      workItem("Overdue schedule", item, `Lag ${fmtAge(item.lag_seconds)}`),
    ),
  ];
  root.innerHTML = items.length ? items.join("") : '<p class="empty">No stuck work.</p>';
}

function renderDevPulse(devPulse) {
  const root = document.querySelector("#dev-pulse");
  if (!devPulse) {
    root.innerHTML = '<p class="empty">No Dev Pulse data.</p>';
    return;
  }
  const last = devPulse.last_success;
  const counts = last?.output?.counts || {};
  root.innerHTML = `
    <div class="detail-row">
      <div class="feed-title">
        <strong>${escapeHtml(devPulse.summary)}</strong>
        ${statusLabel(devPulse.status)}
      </div>
    </div>
    <div class="detail-row">
      <p class="muted">Last success</p>
      <strong>${escapeHtml(last?.workflow_name || "-")}</strong>
      <p class="feed-meta">${fmtTime(last?.completed_at || last?.updated_at)}</p>
    </div>
    <div class="detail-row">
      <p class="muted">Channel</p>
      <strong>${escapeHtml(last?.output?.slack_channel || "-")}</strong>
    </div>
    <div class="detail-row">
      <p class="muted">Counts</p>
      <p>
        Issues closed ${escapeHtml(counts.issues_closed ?? "-")} ·
        created ${escapeHtml(counts.issues_created ?? "-")} ·
        PRs opened ${escapeHtml(counts.prs_opened ?? "-")} ·
        closed ${escapeHtml(counts.prs_closed ?? "-")} ·
        outstanding ${escapeHtml(counts.outstanding_prs ?? "-")}
      </p>
      <p class="feed-meta">
        Non-bug completed ${escapeHtml(counts.non_bug_completed ?? "-")} /
        ${escapeHtml(counts.completion_target ?? "-")}
      </p>
    </div>
    <div class="detail-row">
      <p class="muted">Next schedules</p>
      ${(devPulse.schedules || [])
        .map(
          (schedule) =>
            `<p>${escapeHtml(schedule.workflow_name)} · ${fmtTime(schedule.next_run_at)}</p>`,
        )
        .join("") || "<p>-</p>"}
    </div>
  `;
}

function metric(label, value) {
  return `
    <div class="metric">
      <span class="stat-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(value ?? 0)}</strong>
    </div>
  `;
}

function renderMetrics(data) {
  const root = document.querySelector("#metrics");
  const workflows = data.metrics?.workflow_runs_24h || {};
  const executions = data.metrics?.agent_executions_24h || {};
  const runtime = data.metrics?.runtime || {};
  const sandboxes = runtime.sandbox_sessions || {};
  const observations = data.observations || {};
  root.innerHTML = [
    metric("Workflows failed", workflows.failed || 0),
    metric("Workflows completed", workflows.completed || 0),
    metric("Executions failed", executions.failed_permanent || 0),
    metric("Executions completed", executions.completed || 0),
    metric("Sandboxes active", (sandboxes.running || 0) + (sandboxes.idle || 0)),
    metric("Sandboxes error", sandboxes.error || 0),
    metric("Tool errors", observations.tool_error_events_24h || 0),
    metric("Command errors", observations.command_error_events_24h || 0),
  ].join("");
}

function table(caption, rows, columns) {
  if (!rows?.length) {
    return `<p class="empty">${escapeHtml(caption)}: no rows.</p>`;
  }
  return `
    <table>
      <caption>${escapeHtml(caption)}</caption>
      <thead>
        <tr>${columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("")}</tr>
      </thead>
      <tbody>
        ${rows
          .map(
            (row) => `
              <tr>
                ${columns
                  .map((column) => `<td>${escapeHtml(column.value(row))}</td>`)
                  .join("")}
              </tr>
            `,
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderRecent(data) {
  document.querySelector("#recent-workflows").innerHTML = table(
    "Workflow runs",
    data.recent_workflows || [],
    [
      { label: "Workflow", value: (row) => row.workflow_name },
      { label: "Status", value: (row) => row.status },
      { label: "Run", value: (row) => row.run_id },
      { label: "Updated", value: (row) => fmtTime(row.updated_at) },
    ],
  );
  document.querySelector("#recent-executions").innerHTML = table(
    "Agent executions",
    data.recent_executions || [],
    [
      { label: "Status", value: (row) => row.status },
      { label: "Execution", value: (row) => row.execution_id },
      { label: "Thread", value: (row) => row.thread_key },
      { label: "Updated", value: (row) => fmtTime(row.updated_at) },
    ],
  );
}

function render(data) {
  const status = String(data.status || "ok");
  const overall = document.querySelector("#overall-status");
  overall.textContent = status.toUpperCase();
  overall.className = `status-pill ${status}`;
  setText("#generated-at", fmtTime(data.generated_at));
  setText("#error-count", String(data.recent_errors?.length || 0));
  const stuck = data.stuck_work || {};
  const stuckCount = [
    "running_workflows",
    "overdue_workflows",
    "queued_executions",
    "running_executions",
    "deliveries",
    "schedule_lag",
  ].reduce((total, key) => total + (stuck[key]?.length || 0), 0);
  setText("#stuck-count", String(stuckCount));
  setText("#monitor-summary", `${data.monitors?.length || 0} monitors checked`);
  renderMonitors(data.monitors || []);
  renderErrors(data.recent_errors || []);
  renderStuckWork(data.stuck_work || {});
  renderDevPulse(data.dev_pulse);
  renderMetrics(data);
  renderRecent(data);
}

async function loadSummary() {
  const apiKey = keyInput.value.trim() || sessionStorage.getItem("centaur.ops.apiKey") || "";
  if (!apiKey) {
    setText("#monitor-summary", "Enter an admin API key to load operational data.");
    return;
  }

  const params = new URLSearchParams({ window_hours: windowSelect.value });
  refreshButton.disabled = true;
  try {
    const response = await fetch(`/ops/api/summary?${params.toString()}`, {
      headers: { Authorization: `Bearer ${apiKey}` },
    });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    const data = await response.json();
    state.data = data;
    render(data);
  } catch (error) {
    document.querySelector("#overall-status").textContent = "ERROR";
    setText("#monitor-summary", `Unable to load ops data: ${error.message}`);
  } finally {
    refreshButton.disabled = false;
  }
}

loadSummary();
state.timer = window.setInterval(loadSummary, 30000);
