(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const VIEWS = new Set([
    "overview", "objectives", "inquisitor", "builder", "shape", "settings", "work",
  ]);
  const VIEW_SECTIONS = {
    overview: ["overview"],
    objectives: ["objectives"],
    inquisitor: ["inquisitor"],
    builder: ["builder"],
    work: ["work"],
  };
  const OLD_VIEWS = { today: "objectives", queue: "builder", runs: "builder" };
  const ACTIVE_STATES = new Set([
    "queued", "running", "gate_failed", "policy_failed",
    "review_changes", "merging",
  ]);
  const TERMINAL_STATES = new Set(["merged", "blocked", "error", "cancelled"]);
  const state = {
    token: localStorage.getItem("cc.session") || "",
    view: "today",
    projects: [],
    tasks: [],
    models: [],
    roles: {},
    focus: { entries: [] },
    agenda: null,
    agendaDate: todayISO(),
    agendaExists: false,
    agendaDirty: false,
    objectiveHorizon: "today",
    overview: null,
    overviewAnswer: null,
    overviewError: "",
    horizonCache: null,
    runs: [],
    selectedRun: null,
    builderTab: "stream",
    builderSpec: null,
    builderSpecLoading: null,
    builderSpecTab: "md",
    builderSpecExpanded: [],
    builderBuild: null,
    builderBuildProject: null,
    builderBuildTimer: null,
    builderFollow: true,
    streamEntries: [],
    streamRaw: "",
    builderTimer: null,
    builderClock: null,
    logOffset: 0,
    logAttempt: null,
    blockedRuns: [],
    sse: null,
    taskPicked: null,
    workTask: null,
    workAgendaItem: null,
    workAgendaIndex: null,
    workShape: null,
    workQuestions: null,
    workJobs: [],
    workDiff: "",
    workError: "",
    workBusy: false,
    workRefreshing: false,
    workModel: localStorage.getItem("cc.model") || "",
    workReviewer: localStorage.getItem("cc.reviewer") || "",
    shapeProject: null,
    shapeSeed: null,
    shapeData: null,
    shapeTree: null,
    shapeTreeSlug: null,
    shapeDiff: "",
    shapeError: "",
    specDocs: null,
    useCases: null,
    c4: null,
    specTab: "md",
    useStep: null,
    useDirty: false,
    c4Element: null,
    c4Dirty: false,
    expanded: [],
    activeUseCase: null,
    specKey: null,
    shapeBusy: false,
    shapeTimer: null,
    shapeJobs: [],
    shapeMode: "discuss",
    shapeClock: null,
    workTimer: null,
    focusSearch: "",
    createParent: null,
    createInline: null,
    createOpen: [],
    createSearch: "",
    agendaOpen: [],
  };

  function todayISO() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function formatDay(iso) {
    const d = new Date(`${iso}T00:00:00`);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function flash(message, bad = false) {
    const box = $("flash");
    box.textContent = message;
    box.className = `flash${bad ? " bad" : ""}`;
    box.hidden = false;
    clearTimeout(flash.timer);
    flash.timer = setTimeout(() => { box.hidden = true; }, 4500);
  }

  function badge(value) {
    const text = String(value || "unknown");
    let cls = "st-parked";
    if (["merged", "done", "approve", "shipped", "live"].includes(text)) cls = "st-shipped";
    else if (["blocked", "error", "cancelled", "rejected", "block", "policy_failed", "gate_failed", "no_diff", "review_block", "review_changes"].includes(text)) cls = "st-stalled";
    else if (["running", "queued", "review", "merging", "doing", "planned", "draft", "needs spec"].includes(text)) cls = "st-gated";
    else if (["todo", "shape", "parked"].includes(text)) cls = "st-parked";
    return `<span class="badge ${cls}">${esc(text)}</span>`;
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const request = { ...options, headers };
    if (request.body && typeof request.body !== "string") {
      headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(request.body);
    }
    const response = await fetch(path, request);
    if (response.status === 401) {
      localStorage.removeItem("cc.session");
      state.token = "";
      showLogin("Session expired. Enter the PIN.");
      throw new Error("unauthorized");
    }
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.detail : null;
      const error = new Error(
        typeof detail === "string" ? detail : data && typeof data === "object" && data.code ? data.code : `HTTP ${response.status}`
      );
      error.payload = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function showLogin(message = "") {
    if (state.sse) { state.sse.close(); state.sse = null; }
    stopBuilderPoll();
    stopWorkPoll();
    $("flash").hidden = true;
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    $("login-view").hidden = false;
    $("nav").hidden = true;
    $("logout").hidden = true;
    $("settings-open").hidden = true;
    $("status-strip").hidden = true;
    document.querySelectorAll("main.app-main > section.view").forEach((section) => {
      section.hidden = true;
    });
    $("conn").textContent = "locked";
    $("conn").className = "badge st-parked";
    $("pin-msg").textContent = message;
    if (navigator.userActivation) $("pin-input").focus();
  }

  function showApp() {
    $("login-view").hidden = true;
    $("nav").hidden = false;
    $("logout").hidden = false;
    $("settings-open").hidden = false;
    $("status-strip").hidden = false;
    $("conn").textContent = "online";
    $("conn").className = "badge st-live";
    $("pin-input").value = "";
    $("pin-msg").textContent = "";
  }

  async function connect() {
    const pin = $("pin-input").value.trim();
    if (!/^\d{4,8}$/.test(pin)) {
      showLogin("Enter the 4–8 digit PIN.");
      return;
    }
    try {
      const response = await fetch("/api/auth/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        showLogin(data.detail || "PIN rejected.");
        return;
      }
      state.token = data.token;
      localStorage.setItem("cc.session", state.token);
      await boot();
    } catch (error) {
      showLogin(error.message);
    }
  }

  function logout() {
    localStorage.removeItem("cc.session");
    state.token = "";
    state.selectedRun = null;
    showLogin("Board locked.");
    history.replaceState(null, "", location.pathname);
  }

  async function boot() {
    const [projects, models, settings, tasks, focus, runs] = await Promise.all([
      api("/api/projects"),
      api("/api/models"),
      api("/api/settings"),
      api("/api/tasks"),
      api("/api/focus"),
      api("/api/runs"),
    ]);
    state.projects = projects.projects || [];
    state.models = models.models || [];
    state.roles = settings.roles || {};
    state.tasks = tasks.tasks || [];
    state.focus = focus || { entries: [] };
    state.runs = runs || [];
    showApp();
    renderFocus();
    renderBuilder();
    await loadAgenda();
    refreshStatus();
    const raw = location.hash.replace(/^#/, "");
    const [base, taskPart] = raw.split("/");
    if (base === "work" && taskPart) showWorkById(decodeURIComponent(taskPart));
    else if (base === "shape" && taskPart) openShapePage(decodeURIComponent(taskPart));
    else showView(base || "overview");
    connectSSE();
  }

  function projectName(id) {
    return state.projects.find((project) => project.id === id)?.name || id;
  }

  function objectiveLabel(task) {
    return task?.objective_title || task?.objective || "objective";
  }

  function projectById(id) {
    return state.projects.find((project) => project.id === id) || null;
  }

  function modelLabel(modelId) {
    return state.models.find((model) => model.id === modelId)?.label || modelId || "";
  }

  function roleModelLabel(role) {
    return modelLabel(state.roles?.[role]);
  }

  function buildModels() {
    // The reviewer is whatever the operator configured; it may be the same
    // model as the coder, so nothing is filtered out here.
    return state.models.filter((model) => model.up);
  }

  function modelCards(models, name, selected, disabled = false) {
    return models
      .map((model) => `
        <label class="work-model" title="${esc(model.reason || "")}">
          <input type="radio" name="${esc(name)}" value="${esc(model.id)}"
            ${model.id === selected ? "checked" : ""} ${disabled ? "disabled" : ""}>
          <span class="work-model-text">
            <span class="work-model-name">${esc(model.label)}</span>
            <span class="work-model-state">${esc(model.reason || model.model || "")}</span>
          </span>
        </label>`)
      .join("");
  }

  function taskById(id) {
    return state.tasks.find((task) => task.task === id);
  }

  function activeRuns() {
    return state.runs.filter((run) => ACTIVE_STATES.has(run.state));
  }

  function refreshStatus() {
    const running = activeRuns().length;
    const blocked = state.runs.filter((run) => run.state === "blocked").length;
    $("status-running").textContent = running;
    $("status-blocked").textContent = blocked;
    $("status-note").textContent = `synced ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  }

  function showView(view) {
    if (OLD_VIEWS[view]) view = OLD_VIEWS[view];
    if (!VIEWS.has(view)) view = "overview";
    if (view === "work" && !state.workTask) view = "overview";
    if (view === "shape" && !state.shapeProject) view = "overview";
    if (view !== "shape") {
      setTreeFullscreen(false);
      if (state.shapeTimer) {
        clearInterval(state.shapeTimer);
        state.shapeTimer = null;
      }
      stopShapeClock();
    }
    if (state.view === "work" && view !== "work") stopWorkPoll();
    state.view = view;
    const sections = VIEW_SECTIONS[view] || [view];
    document.querySelectorAll("main.app-main > section.view").forEach((section) => {
      section.hidden = !sections.some((name) => section.id === `view-${name}`);
    });
    const navView = view === "work" ? "objectives" : view === "shape" ? "inquisitor" : view;
    document.querySelectorAll("#nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === navView);
    });
    if (view === "work") {
      window.scrollTo(0, 0);
      renderWork();
      return;
    }
    if (view === "objectives") renderObjectives();
    if (view === "builder") {
      renderBuilder();
      resumeBuilderSession();
    }
    if (view === "overview") loadOverview();
    if (view === "inquisitor") renderInquisitor();
    if (view === "settings") renderSettings();
  }

  function go(view) {
    if (location.hash === `#${view}`) showView(view);
    else location.hash = view;
  }

  // ---------------------------------------------------------------- today
  async function loadAgenda() {
    const day = $("agenda-date").value || state.agendaDate || todayISO();
    state.agendaDate = day;
    state.horizonCache = null;
    try {
      state.agenda = await api(`/api/agendas/${day}`);
      state.agendaExists = true;
    } catch (error) {
      if (error.status === 404) {
        state.agendaExists = false;
        state.agenda = {
          date: day,
          status: "draft",
          rev: 0,
          focus: [],
          capacity: { work_items: 3, chores: 3, operator_min: 90 },
          items: [],
        };
      } else {
        flash(error.message, true);
        return;
      }
    }
    state.agendaDirty = false;
    renderToday();
  }

  function renderObjectives() {
    const horizon = state.objectiveHorizon || "today";
    document.querySelectorAll("#objective-segments button").forEach((button) => {
      button.classList.toggle("active", button.dataset.horizon === horizon);
    });
    $("objectives-today").hidden = horizon !== "today";
    $("objectives-other").hidden = horizon === "today";
    if (horizon === "today") renderToday();
    else loadHorizon(horizon);
  }

  function isoDay(date) {
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${date.getFullYear()}-${month}-${day}`;
  }

  function horizonDays(horizon) {
    const base = new Date(`${state.agendaDate}T12:00:00`);
    const days = [];
    if (horizon === "week") {
      const offset = (base.getDay() + 6) % 7;
      for (let index = 0; index < 7; index += 1) {
        const day = new Date(base);
        day.setDate(base.getDate() - offset + index);
        days.push(isoDay(day));
      }
      return days;
    }
    const last = new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
    for (let index = 1; index <= last; index += 1) {
      days.push(isoDay(new Date(base.getFullYear(), base.getMonth(), index)));
    }
    return days;
  }

  function objectiveTitle(id) {
    const task = state.tasks.find((item) => item.objective === id);
    return task?.objective_title || id;
  }

  function renderHorizon(horizon) {
    const rows = [...(state.horizonCache?.rows || [])];
    const label = horizon === "week" ? "this week" : "this month";
    if (!rows.length) {
      $("objectives-other").innerHTML = `<p class="muted">No agenda items planned ${label}.</p>`;
      return;
    }
    rows.sort((a, b) => b.items.length - a.items.length);
    $("objectives-other").innerHTML = `<div class="builder-list">${rows.map((row) => {
      const lines = row.items
        .slice(0, 3)
        .map((item) => `<div class="builder-meta">${esc(item.date)} · ${esc(taskById(item.task)?.title || item.task)}</div>`)
        .join("");
      return `<div class="builder-row">
        <div class="builder-main">
          <div class="builder-title">${esc(objectiveTitle(row.objective))}</div>
          <div class="builder-meta">${esc(projectName(row.project))} · ${row.items.length} item${row.items.length === 1 ? "" : "s"}</div>
          ${lines}
        </div>
        <div class="builder-side">${badge(row.items.length)}</div>
      </div>`;
    }).join("")}</div>`;
  }

  async function loadHorizon(horizon) {
    const days = horizonDays(horizon);
    const key = `${horizon}:${days[0]}:${state.agenda?.rev ?? ""}`;
    if (state.horizonCache?.key === key) {
      renderHorizon(horizon);
      return;
    }
    $("objectives-other").innerHTML = '<p class="muted">Reading agendas…</p>';
    const agendas = await Promise.all(
      days.map(async (day) => {
        try {
          return await api(`/api/agendas/${day}`);
        } catch (error) {
          return null;
        }
      })
    );
    const rows = new Map();
    for (const agenda of agendas) {
      if (!agenda) continue;
      for (const item of agenda.items || []) {
        const rowKey = `${item.project}|${item.objective}`;
        const row = rows.get(rowKey)
          || { project: item.project, objective: item.objective, items: [] };
        row.items.push({ ...item, date: agenda.date });
        rows.set(rowKey, row);
      }
    }
    state.horizonCache = { key, rows: [...rows.values()] };
    renderHorizon(horizon);
  }

  async function loadOverview() {
    try {
      const data = await api("/api/overview");
      state.overview = data.rows || [];
      state.overviewError = "";
    } catch (error) {
      state.overview = [];
      state.overviewError = error.message;
    }
    renderOverview();
  }

  function renderOverview() {
    const rows = state.overview || [];
    const items = state.agenda?.items || [];
    const locked = state.agenda?.status === "locked";
    const urgent = rows.filter((row) => row.severity === "high").length;
    $("overview-sub").textContent = state.overviewError
      ? state.overviewError
      : rows.length
        ? `${urgent} urgent · ${rows.length} action${rows.length === 1 ? "" : "s"}`
        : "Nothing is waiting on you";
    $("overview-list").innerHTML = rows.map((row) => {
      const expanded = state.overviewAnswer === row.id;
      const buttons = (row.actions || []).map((action) => `
        <button class="sy-btn ${["answer", "approve", "save-answers"].includes(action.id) ? "primary" : "ghost"}"
          data-ov-action="${esc(action.id)}" data-ov-row="${esc(row.id)}">${esc(action.label)}</button>`).join("");
      const questions = expanded && (row.questions || []).length
        ? `<div class="ov-answers">${row.questions.map((question) => `
            <label class="ov-question">
              <span>${esc(question.question)}</span>
              <input class="sy-input" data-ov-q="${esc(question.id)}"
                value="${esc(question.answer || "")}" placeholder="${esc(question.default || "")}">
            </label>`).join("")}
            <div class="ov-answer-actions">
              <button class="sy-btn primary" data-ov-action="save-answers" data-ov-row="${esc(row.id)}">Save answers & revise</button>
            </div>
          </div>`
        : "";
      return `<div class="action-row ov-row ${esc(row.severity)}">
        <div class="action-main">
          <div class="action-title">${esc(row.title)}</div>
          <div class="action-meta">${esc(row.detail || "")}</div>
          ${questions}
        </div>
        <div class="action-side">${buttons}</div>
      </div>`;
    }).join("");
    $("overview-list").hidden = rows.length === 0;
    $("overview-empty").hidden = rows.length > 0 || Boolean(state.overviewError);
    $("overview-today-badge").innerHTML = state.agendaExists
      ? badge(locked ? "locked" : "draft")
      : badge("none");
    const done = items.filter((item) => item.status === "done").length;
    $("overview-today").textContent = items.length
      ? `${items.length} item${items.length === 1 ? "" : "s"} · ${done} done${locked ? " · locked" : ""}`
      : "No agenda for today.";
  }

  async function runOverviewAction(action, row) {
    if (action === "answer") {
      state.overviewAnswer = state.overviewAnswer === row.id ? null : row.id;
      renderOverview();
      return;
    }
    if (action === "compose") {
      go("objectives");
      return;
    }
    if (action === "open" && row.task) {
      showWorkById(row.task);
      return;
    }
    if (action === "inspect" && row.run) {
      go("builder");
      await selectRun(row.run);
      return;
    }
    try {
      if (action === "save-answers") {
        const answers = {};
        for (const input of $("overview-list").querySelectorAll("[data-ov-q]")) {
          const value = input.value.trim();
          if (value) answers[input.dataset.ovQ] = value;
        }
        if (!Object.keys(answers).length) {
          flash("Type at least one answer.", true);
          return;
        }
        await api(`/api/shapes/${encodeURIComponent(row.project)}/jobs`, {
          method: "POST",
          body: { kind: "answers", answers },
        });
        state.overviewAnswer = null;
        flash("Answers queued. The inquisitor is revising.");
      } else if (action === "revise") {
        await api(`/api/shapes/${encodeURIComponent(row.project)}/jobs`, {
          method: "POST",
          body: { kind: "revise" },
        });
        flash("Revision queued.");
      } else if (action === "materialize") {
        await api(`/api/shapes/${encodeURIComponent(row.project)}/materialize`, {
          method: "POST",
        });
        flash("Materialized.");
      } else if (action === "approve") {
        await api(`/api/shapes/${encodeURIComponent(row.project)}/approve`, {
          method: "POST",
        });
        flash("Spec approved.");
      } else if (action === "lock") {
        await api(`/api/agendas/${state.agendaDate}/lock`, {
          method: "POST",
          body: { if_rev: state.agenda?.rev || 0 },
        });
        flash("Agenda locked.");
      } else if (action === "retry" && row.task) {
        const run = await api("/api/runs", {
          method: "POST",
          body: { task: row.task },
        });
        go("builder");
        await selectRun(run.id);
        return;
      }
      await loadOverview();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function inquisitorRows() {
    const rows = [];
    for (const item of state.agenda?.items || []) {
      const task = taskById(item.task);
      if (!task) continue;
      const codes = (task.reasons || []).map((reason) => reason.code);
      const code = codes.find(
        (value) => value === "spec_unapproved" || value === "no_gate"
      );
      if (!code) continue;
      rows.push({
        task,
        code,
        project: projectName(item.project),
        projectId: item.project,
        objectiveId: item.objective,
      });
    }
    return rows;
  }

  function renderSettings() {
    const roles = ["inquisitor", "coder", "reviewer", "chat"];
    const labels = {
      inquisitor: "Inquisitor (shaping)",
      coder: "Build (coder)",
      reviewer: "Reviewer",
      chat: "Chat",
    };
    const models = state.models || [];
    $("settings-sub").textContent = state.roles
      ? `config.yaml · ${models.length} model${models.length === 1 ? "" : "s"}`
      : "model definitions are read-only here";
    $("settings-roles").innerHTML = roles
      .map(
        (role) => `<label class="sy-field">
          <span>${esc(labels[role])}</span>
          <select class="sy-select" data-role="${esc(role)}">
            ${models
              .map(
                (model) =>
                  `<option value="${esc(model.id)}"${state.roles?.[role] === model.id ? " selected" : ""}>${esc(model.label)}${model.up ? "" : " (down)"}</option>`
              )
              .join("")}
          </select>
        </label>`
      )
      .join("");
    $("settings-models").innerHTML = models
      .map(
        (model) => `<div class="settings-model">
          <span class="settings-model-name">${esc(model.label)}</span>
          <span class="settings-model-meta">${esc(
            model.context_window
              ? `${(model.context_window / 1048576).toFixed(model.context_window % 1048576 ? 1 : 0)}M window`
              : "window unknown"
          )} · ${esc(model.model)}</span>
          <span class="badge ${model.up ? "st-live" : "st-stalled"}">${model.up ? "up" : "down"}</span>
        </div>`
      )
      .join("");
  }

  async function saveSettings() {
    const roles = {};
    document.querySelectorAll("#settings-roles [data-role]").forEach((select) => {
      roles[select.dataset.role] = select.value;
    });
    if (!Object.keys(roles).length) return;
    try {
      const result = await api("/api/settings/roles", {
        method: "PUT",
        body: roles,
      });
      state.roles = result.roles || state.roles;
      flash("Role defaults saved.");
      renderSettings();
      renderWorkModel();
      renderShapeModel();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function renderInquisitor() {
    const rows = inquisitorRows();
    const items = state.agenda?.items || [];
    $("inq-sub").textContent = rows.length
      ? `${rows.length} of ${items.length} items need a spec revision`
      : "Today's agenda is clean";
    $("inq-list").innerHTML = rows.map((row) => `
      <div class="action-row">
        <div class="action-main">
          <div class="action-title">${esc(row.task.title)}</div>
          <div class="action-meta">${esc(row.project)} · ${esc(row.code)}</div>
        </div>
        <div class="action-side">
          ${badge("needs spec")}
          <button class="sy-btn ghost" data-inq-project="${esc(row.projectId)}" data-inq-objective="${esc(row.objectiveId)}" data-inq-task="${esc(row.task.task)}">Open</button>
        </div>
      </div>`).join("");
    $("inq-list").hidden = rows.length === 0;
    $("inq-empty").hidden = rows.length > 0;
  }

  function renderToday() {
    const agenda = state.agenda;
    if (!agenda) return;
    $("agenda-date").value = state.agendaDate;
    $("today-sub").textContent = `${formatDay(state.agendaDate)} · ${state.agendaExists ? agenda.status : "no agenda"} · rev ${agenda.rev}`;
    $("agenda-state").innerHTML = `${badge(agenda.status)} <span class="muted">rev ${agenda.rev}</span>`;
    const locked = agenda.status === "locked";
    const items = agenda.items || [];
    const done = items.filter((item) => item.status === "done").length;
    const projects = new Set(items.map((item) => item.project)).size;
    $("today-kpis").innerHTML = [
      [String(projects), projects === 1 ? "project" : "projects"],
      [String(items.length), "tasks"],
      [String(done), "done"],
      [String(state.focus.entries.length), "focus"],
      [String(agenda.capacity?.work_items ?? 3), "work cap"],
    ].map(([value, label]) => `<div class="kpi"><b>${esc(value)}</b><span>${esc(label)}</span></div>`).join("");
    renderTodayActions();
    $("agenda-save").disabled = locked || !state.agendaDirty;
    $("agenda-save").textContent = state.agendaDirty ? "Save" : "Saved";
    $("add-task").disabled = locked;
    $("focus-edit").disabled = locked;
    renderFocus();
    renderAgendaRows();
  }

  function renderTodayActions() {
    const agenda = state.agenda;
    const primary = $("today-primary");
    const secondary = $("today-secondary");
    const active = activeRuns()[0];
    const draftWithItems = state.agendaExists && agenda.status === "draft" && (agenda.items || []).length > 0;
    let action = "suggest";
    let label = "Suggest draft";
    if (active) {
      action = "run";
      label = "View active run";
    } else if (draftWithItems) {
      action = "lock";
      label = "Lock today";
    } else if (state.agendaExists && agenda.status === "locked") {
      action = "queue";
      label = "Open builder";
    } else if (state.agendaExists) {
      action = "suggest";
      label = "Suggest items";
    }
    primary.dataset.action = action;
    primary.textContent = label;
    secondary.hidden = true;
    secondary.dataset.action = "";
    secondary.textContent = "";
    if (agenda.status === "draft" && (agenda.items || []).length) {
      secondary.hidden = false;
      secondary.dataset.action = "suggest";
      secondary.textContent = "Suggest again";
    } else if (agenda.status === "locked") {
      secondary.hidden = false;
      secondary.dataset.action = "unlock";
      secondary.textContent = "Unlock";
    }
  }

  function renderAgendaRows() {
    const agenda = state.agenda;
    if (!agenda) return;
    const locked = agenda.status === "locked";
    const items = agenda.items || [];
    $("agenda-empty").hidden = items.length > 0;
    if (!items.length) {
      $("agenda-list").innerHTML = "";
      return;
    }
    const groups = [];
    for (const item of items) {
      let group = groups[groups.length - 1];
      if (!group || group.project !== item.project) {
        group = { project: item.project, items: [] };
        groups.push(group);
      }
      group.items.push(item);
    }
    const objectiveTitle = (item) => {
      const row = state.tasks.find(
        (task) => task.project === item.project && task.objective === item.objective
      );
      return row?.objective_title || item.objective;
    };
    const parts = [];
    let index = 0;
    for (const group of groups) {
      const total = group.items.length;
      const done = group.items.filter((item) => item.status === "done").length;
      const open = state.agendaOpen.includes(group.project);
      parts.push(`<button type="button" class="agenda-group project agenda-project-toggle${open ? " open" : ""}"
          data-agenda-project="${esc(group.project)}">
        <span class="chev" aria-hidden="true"></span>
        <span class="group-name">${esc(projectName(group.project))}</span>
        <span class="group-count">${total} task${total === 1 ? "" : "s"}${
          done ? ` · ${done} done` : ""
        }</span>
      </button>`);
      if (!open) continue;
      let previousObjective = null;
      for (const item of group.items) {
        if (item.objective !== previousObjective) {
          const count = group.items.filter(
            (row) => row.objective === item.objective
          ).length;
          parts.push(`<div class="agenda-group objective">
            <span>${esc(objectiveTitle(item))}</span>
            <span>${count}</span>
          </div>`);
          previousObjective = item.objective;
        }
        const task = taskById(item.task);
        const title = task?.title || item.task;
        const reason = locked
          ? `<div class="agenda-reason">${esc(item.reason || "No reason")}</div>`
          : `<input class="agenda-reason" data-reason="${index}" value="${esc(item.reason || "")}" placeholder="Why today?" aria-label="Reason">`;
        const actions = locked ? "" : `
          <div class="agenda-actions">
            <button class="sy-btn ghost" data-agenda-action="up" data-index="${index}" aria-label="Move up">↑</button>
            <button class="sy-btn ghost" data-agenda-action="down" data-index="${index}" aria-label="Move down">↓</button>
            <button class="sy-btn ghost" data-agenda-action="remove" data-index="${index}" aria-label="Remove">×</button>
          </div>`;
        parts.push(`<div class="agenda-row${locked ? " is-locked" : ""}" data-index="${index}">
          <div class="agenda-order">${String(index + 1).padStart(2, "0")}</div>
          <div class="agenda-main">
            <button class="agenda-title-btn" type="button" data-work-index="${index}" title="Open work item">${esc(title)}</button>
            <div class="agenda-meta">${esc(item.task)} · ${esc(item.kind)}</div>
            ${reason}
          </div>
          <div class="agenda-side">
            ${badge(item.status || "planned")}
            ${actions}
          </div>
        </div>`);
        index += 1;
      }
    }
    $("agenda-list").innerHTML = parts.join("");
  }

  function renderFocus() {
    const entries = state.focus.entries || [];
    const locked = state.agenda?.status === "locked";
    $("focus-slots").innerHTML = entries.length
      ? entries.map((entry) => `
        <div class="focus-slot">
          <span class="pos">${entry.position}</span>
          <span class="name">${esc(projectName(entry.project))}</span>
          ${locked ? "" : `<button class="remove" data-focus-remove="${esc(entry.project)}" aria-label="Remove focus">×</button>`}
        </div>`).join("")
      : '<div class="focus-empty">No focus set.</div>';
  }

  function markDirty() {
    state.agendaDirty = true;
    $("agenda-save").disabled = state.agenda?.status === "locked";
    $("agenda-save").textContent = "Save";
  }

  async function saveAgenda() {
    if (!state.agenda || state.agenda.status === "locked") return;
    const focus = state.focus.entries.map((entry) => entry.project).slice(0, 3);
    const items = (state.agenda.items || []).map((item, index) => ({
      id: item.id || undefined,
      kind: item.kind || "work",
      project: item.project,
      objective: item.objective,
      task: item.task,
      reason: item.reason || "",
      estimate_min: item.estimate_min ?? null,
      due_time: item.due_time ?? null,
      order: index,
      note: item.note ?? null,
    }));
    try {
      state.agenda = await api(`/api/agendas/${state.agendaDate}`, {
        method: "PUT",
        body: {
          focus,
          capacity: state.agenda.capacity || { work_items: 3, chores: 3, operator_min: 90 },
          items,
          generated_by: "manual",
          if_rev: state.agenda.rev || 0,
        },
      });
      state.agendaExists = true;
      state.agendaDirty = false;
      renderToday();
      flash("Agenda saved.");
    } catch (error) {
      flash(error.message, true);
    }
  }

  async function ensureDraft() {
    if (state.agendaExists) return;
    await saveAgenda();
    if (!state.agendaExists) throw new Error("Could not create the draft.");
  }

  async function suggestAgenda() {
    if (state.agenda?.status === "locked") {
      flash("Unlock the agenda first.", true);
      return;
    }
    const replace = state.agendaExists && (state.agenda.items || []).length > 0;
    if (replace && !confirm("Replace the current draft with a new suggestion?")) return;
    try {
      state.agenda = await api(`/api/agendas/${state.agendaDate}/suggest`, {
        method: "POST",
        body: { replace },
      });
      state.agendaExists = true;
      state.agendaDirty = false;
      renderToday();
      flash("Draft suggested.");
    } catch (error) {
      flash(error.message, true);
    }
  }

  async function lockAgenda() {
    if (!state.agenda) return;
    if (state.agendaDirty) {
      await saveAgenda();
      if (state.agendaDirty) return;
    }
    try {
      const result = await api(`/api/agendas/${state.agendaDate}/lock`, {
        method: "POST",
        body: { if_rev: state.agenda.rev || 0 },
      });
      state.agenda = result.agenda;
      state.agendaDirty = false;
      renderToday();
      flash(result.warnings?.length ? "Locked with warnings." : "Agenda locked.");
    } catch (error) {
      const issues = error.payload?.issues || [];
      flash(
        issues.length
          ? issues
              .slice(0, 4)
              .map((issue) => `${issue.code} — ${issue.detail}`)
              .join("; ")
          : error.message,
        true
      );
    }
  }

  async function unlockAgenda() {
    const reason = prompt("Unlock reason:");
    if (!reason) return;
    try {
      state.agenda = await api(`/api/agendas/${state.agendaDate}/unlock`, {
        method: "POST",
        body: { reason },
      });
      state.agendaDirty = false;
      renderToday();
      flash("Agenda unlocked.");
    } catch (error) {
      flash(error.message, true);
    }
  }

  async function removeFocus(project) {
    const ids = state.focus.entries.map((entry) => entry.project).filter((id) => id !== project);
    try {
      state.focus = await api("/api/focus", { method: "PUT", body: { projects: ids } });
      renderFocus();
      if (state.agenda && state.agenda.status !== "locked") markDirty();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function openFocusDialog() {
    if (!state.projects.length) { flash("Project list is still loading.", true); return; }
    state.focusSearch = "";
    $("focus-search").value = "";
    renderFocusOptions();
    $("focus-dialog").showModal();
  }

  function renderFocusOptions() {
    const selected = new Set(state.focus.entries.map((entry) => entry.project));
    const positions = new Map((state.focus.entries || []).map((entry) => [entry.project, entry.position]));
    const projects = state.projects
      .filter((project) => project.status !== "archived")
      .filter((project) => `${project.name} ${project.id}`.toLowerCase().includes(state.focusSearch.toLowerCase()))
      .sort((a, b) => (positions.get(a.id) ?? 99) - (positions.get(b.id) ?? 99) || a.name.localeCompare(b.name));
    $("focus-options").innerHTML = projects.map((project) => `
      <label class="focus-option${selected.has(project.id) ? " checked" : ""}">
        <input type="checkbox" value="${esc(project.id)}" ${selected.has(project.id) ? "checked" : ""}>
        <span>${esc(project.name)}</span>
      </label>`).join("") || '<p class="muted">No matching projects.</p>';
    $("focus-count").textContent = `${selected.size}/3`;
  }

  async function saveFocusDialog() {
    const ids = [...document.querySelectorAll("#focus-options input:checked")].map((input) => input.value);
    if (ids.length > 3) { flash("Focus allows at most three projects.", true); return; }
    const positions = new Map((state.focus.entries || []).map((entry) => [entry.project, entry.position]));
    ids.sort((a, b) => (positions.get(a) ?? 99) - (positions.get(b) ?? 99));
    try {
      state.focus = await api("/api/focus", { method: "PUT", body: { projects: ids } });
      renderFocus();
      if (state.agenda && state.agenda.status !== "locked") markDirty();
      $("focus-dialog").close();
      flash("Focus saved.");
    } catch (error) {
      flash(error.message, true);
    }
  }

  // ---------------------------------------------------------------- add
  function openCreateDialog() {
    state.createInline = null;
    state.createSearch = "";
    if ($("create-search")) $("create-search").value = "";
    renderCreateTree();
    $("create-dialog").showModal();
    refreshStructure().catch(() => {});
  }

  function createTreeModel() {
    const groups = new Map();
    for (const project of state.projects) {
      const group = {
        project: { id: project.id, name: project.name || project.id },
        objectives: new Map(),
      };
      for (const objective of project.objectives || []) {
        group.objectives.set(objective.id, {
          id: objective.id,
          title: objective.title || objective.id,
          tasks: [],
        });
      }
      groups.set(project.id, group);
    }
    for (const task of state.tasks) {
      let group = groups.get(task.project);
      if (!group) {
        group = {
          project: { id: task.project, name: projectName(task.project) },
          objectives: new Map(),
        };
        groups.set(task.project, group);
      }
      const key = task.objective || "—";
      if (!group.objectives.has(key)) {
        group.objectives.set(key, {
          id: key,
          title: task.objective_title || key,
          tasks: [],
        });
      }
      group.objectives.get(key).tasks.push(task);
    }
    return [...groups.values()];
  }

  function createObjectiveTitle(projectId, objectiveId) {
    const row = state.tasks.find(
      (task) => task.project === projectId && task.objective === objectiveId
    );
    if (row?.objective_title) return row.objective_title;
    const project = state.projects.find((item) => item.id === projectId);
    return (
      project?.objectives?.find((item) => item.id === objectiveId)?.title ||
      objectiveId
    );
  }

  function createInlineHtml(kind, project, objective) {
    return `<div class="create-inline">
      <input id="create-inline-input" class="sy-input" placeholder="${
        kind === "objective" ? "Objective title" : "Task title"
      }" autocomplete="off">
      <button class="sy-btn primary" type="button" data-create-inline-save="1">Add</button>
      <button class="sy-btn ghost" type="button" data-create-inline-cancel="1">Cancel</button>
    </div>`;
  }

  function createInlineMatches(kind, project, objective) {
    return (
      state.createInline &&
      state.createInline.kind === kind &&
      state.createInline.project === project &&
      (objective == null || state.createInline.objective === objective)
    );
  }

  function renderCreateTree() {
    const box = $("create-tree");
    if (!box) return;
    const model = createTreeModel();
    const selected = state.createParent;
    const needle = state.createSearch.trim().toLowerCase();
    const matches = (text) =>
      !needle || String(text || "").toLowerCase().includes(needle);
    const parts = [];
    let shown = 0;
    for (const group of model) {
      const projectId = group.project.id;
      const objectives = [...group.objectives.values()];
      const projectHit = matches(group.project.name);
      const objectiveHits = objectives.filter(
        (objective) =>
          matches(objective.title) ||
          objective.tasks.some((task) => matches(task.title))
      );
      if (!projectHit && !objectiveHits.length) continue;
      shown += 1;
      const expanded =
        state.createOpen.includes(projectId) || Boolean(needle);
      const visibleObjectives = projectHit ? objectives : objectiveHits;
      const taskCount = objectives.reduce(
        (sum, objective) => sum + objective.tasks.length,
        0
      );
      const projectSelected =
        selected && selected.project === projectId && !selected.objective;
      parts.push(`<div class="create-node create-project-node${
        projectSelected ? " selected" : ""
      }${expanded ? " open" : ""}" data-create-project="${esc(projectId)}">
        <span class="chev" aria-hidden="true"></span>
        <span class="name">${esc(group.project.name)}</span>
        <span class="count">${objectives.length} obj · ${taskCount} tasks</span>
        <span class="create-actions">
          <button class="sy-btn ghost" type="button"
            data-create-add-objective="${esc(projectId)}">+ objective</button>
          <button class="sy-btn ghost" type="button"
            data-create-project-agenda="${esc(projectId)}"
            title="add every open task in this project to the agenda">→ agenda</button>
        </span>
      </div>`);
      if (createInlineMatches("objective", projectId, null)) {
        parts.push(createInlineHtml("objective", projectId, null));
      }
      if (!expanded) continue;
      parts.push('<div class="create-objectives">');
      for (const objective of visibleObjectives) {
        const objectiveSelected =
          selected &&
          selected.project === projectId &&
          selected.objective === objective.id;
        parts.push(`<div class="create-node${objectiveSelected ? " selected" : ""}"
            data-create-objective="${esc(objective.id)}"
            data-create-project-id="${esc(projectId)}">
          <span class="name">${esc(objective.title)}</span>
          <span class="count">${objective.tasks.length}</span>
          <span class="create-actions">
            <button class="sy-btn ghost" type="button"
              data-create-add-task="${esc(objective.id)}"
              data-project="${esc(projectId)}">+ task</button>
            <button class="sy-btn ghost" type="button"
              data-create-objective-agenda="${esc(objective.id)}"
              data-project="${esc(projectId)}"
              title="add every open task in this objective to the agenda">→ agenda</button>
          </span>
        </div>`);
        if (createInlineMatches("task", projectId, objective.id)) {
          parts.push(createInlineHtml("task", projectId, objective.id));
        }
        parts.push(
          `<div class="create-tasks">${objective.tasks
            .filter((task) => projectHit || matches(task.title) || !needle)
            .map((task) => {
              const queued = (state.agenda?.items || []).some(
                (item) => item.task === task.task
              );
              const done = task.status === "done";
              return `<button type="button" class="create-task-row${
                done ? " done" : ""
              }${queued ? " in-agenda" : ""}" data-create-task-agenda="${esc(
                task.task
              )}" data-project="${esc(projectId)}" data-objective="${esc(
                objective.id
              )}" ${done ? "disabled" : ""}>
                <span>${esc(task.title)}</span>
                <span class="mark">${done ? "done" : queued ? "on agenda" : "add"}</span>
              </button>`;
            })
            .join("")}</div>`
        );
      }
      parts.push("</div>");
    }
    if (!shown) {
      parts.push(
        '<p class="muted">Nothing matches that filter.</p>'
      );
    }
    box.innerHTML = parts.join("");
    $("create-parent-label").textContent = selected
      ? selected.objective
        ? `task in ${projectName(selected.project)} · ${createObjectiveTitle(
            selected.project,
            selected.objective
          )}`
        : `objective in ${projectName(selected.project)}`
      : "nothing selected";
    $("create-task-save").disabled = !selected?.objective;
    const inline = $("create-inline-input");
    if (inline) inline.focus();
  }

  function toggleCreateProject(projectId) {
    const at = state.createOpen.indexOf(projectId);
    if (at >= 0) state.createOpen.splice(at, 1);
    else state.createOpen.push(projectId);
    state.createParent = { project: projectId, objective: null };
    renderCreateTree();
  }

  async function refreshStructure() {
    const [tasks, projects] = await Promise.all([
      api("/api/tasks"),
      api("/api/projects"),
    ]);
    state.tasks = tasks.tasks || [];
    state.projects = projects.projects || [];
    renderCreateTree();
    if (state.view === "objectives") renderToday();
    if (state.view === "builder") renderBuilder();
    refreshStatus();
  }

  async function createProject() {
    const name = $("create-project-name").value.trim();
    const path = $("create-project-path").value.trim();
    if (!name) {
      flash("Project name is required.", true);
      return;
    }
    try {
      const result = await api("/api/projects", {
        method: "POST",
        body: { name, path },
      });
      $("create-project-name").value = "";
      $("create-project-path").value = "";
      state.createParent = { project: result.project.id, objective: null };
      await refreshStructure();
      flash(
        path
          ? "Project created."
          : "Project created — add a path before shaping it."
      );
    } catch (error) {
      flash(error.message, true);
    }
  }

  function taskRefs(tasks) {
    return tasks
      .filter((task) => task.status !== "done")
      .map((task) => ({
        project: task.project,
        objective: task.objective,
        task: task.task,
      }));
  }

  async function addTasksToAgenda(refs) {
    if (!refs.length) {
      flash("Nothing open to add there.", true);
      return;
    }
    try {
      await ensureDraft();
      if (state.agendaDirty) {
        await saveAgenda();
        if (state.agendaDirty) return;
      }
      const result = await api(`/api/agendas/${state.agendaDate}/tasks`, {
        method: "POST",
        body: { tasks: refs, if_rev: state.agenda?.rev ?? 0 },
      });
      state.agenda = result.agenda;
      state.agendaExists = true;
      state.agendaDirty = false;
      renderToday();
      renderCreateTree();
      flash(
        `${result.added} added${
          result.skipped ? ` · ${result.skipped} already there or gone` : ""
        }.`
      );
    } catch (error) {
      flash(error.message, true);
    }
  }

  function addProjectToAgenda(projectId) {
    addTasksToAgenda(
      taskRefs(state.tasks.filter((task) => task.project === projectId))
    );
  }

  function addObjectiveToAgenda(projectId, objectiveId) {
    addTasksToAgenda(
      taskRefs(
        state.tasks.filter(
          (task) =>
            task.project === projectId && task.objective === objectiveId
        )
      )
    );
  }

  async function createTask(project, objective, title) {
    const addToday = $("create-task-today").checked;
    let day = null;
    let rev = null;
    if (addToday) {
      await ensureDraft();
      if (state.agendaDirty) {
        await saveAgenda();
        if (state.agendaDirty) return false;
      }
      day = state.agendaDate;
      rev = state.agenda?.rev ?? 0;
    }
    const result = await api("/api/tasks", {
      method: "POST",
      body: {
        project,
        objective,
        title,
        add_to_today: addToday,
        day,
        if_rev: rev,
      },
    });
    if (result.agenda) {
      state.agenda = result.agenda;
      state.agendaExists = true;
      state.agendaDirty = false;
      renderToday();
    }
    await refreshStructure();
    return true;
  }

  async function submitCreateInline() {
    const inline = state.createInline;
    const input = $("create-inline-input");
    if (!inline || !input) return;
    const title = input.value.trim();
    if (!title) {
      flash("Title is required.", true);
      return;
    }
    try {
      if (inline.kind === "objective") {
        const result = await api(
          `/api/projects/${encodeURIComponent(inline.project)}/objectives`,
          { method: "POST", body: { title } }
        );
        state.createParent = {
          project: inline.project,
          objective: result.objective.id,
        };
        state.createInline = null;
        await refreshStructure();
        flash("Objective created.");
      } else {
        const saved = await createTask(inline.project, inline.objective, title);
        if (!saved) return;
        state.createInline = null;
        renderCreateTree();
        flash("Task created.");
      }
    } catch (error) {
      flash(error.message, true);
    }
  }

  async function createTaskFromForm() {
    const parent = state.createParent;
    const title = $("create-task-title").value.trim();
    if (!parent?.objective) {
      flash("Pick an objective in the tree first.", true);
      return;
    }
    if (!title) {
      flash("Task title is required.", true);
      return;
    }
    try {
      const saved = await createTask(parent.project, parent.objective, title);
      if (!saved) return;
      $("create-task-title").value = "";
      flash("Task created.");
    } catch (error) {
      flash(error.message, true);
    }
  }

  // ---------------------------------------------------------------- work item
  function currentWorkTask() {
    if (!state.workTask) return null;
    return taskById(state.workTask.task) || state.workTask;
  }

  function workErrorText(error) {
    const payload = error?.payload;
    if (payload && Array.isArray(payload.issues) && payload.issues.length) {
      return payload.issues
        .map((issue) => `${issue.code || "issue"}: ${issue.detail || ""}`)
        .join("\n");
    }
    if (payload && typeof payload.detail === "string") return payload.detail;
    return error?.message || "Unexpected error.";
  }

  function showWorkError(message) {
    state.workError = message || "";
    renderWork();
  }

  function workLatestRun(taskId) {
    return [...state.runs]
      .filter((run) => run.task === taskId)
      .sort((a, b) => String(b.created).localeCompare(String(a.created)))[0] || null;
  }

  function workActiveRun(taskId) {
    return state.runs.find((run) => run.task === taskId && ACTIVE_STATES.has(run.state)) || null;
  }

  function openWorkSheet(task, agendaItem = null, agendaIndex = null, options = {}) {
    if (!task) return;
    state.workTask = task;
    state.workAgendaItem = agendaItem;
    state.workAgendaIndex = agendaIndex;
    state.workShape = null;
    state.workQuestions = null;
    state.workJobs = [];
    state.workDiff = "";
    state.workError = "";
    state.workBusy = false;
    const target = `work/${encodeURIComponent(task.task)}`;
    if (options.route !== false && location.hash.replace(/^#/, "") !== target) {
      location.hash = target;
      return;
    }
    showView("work");
    renderWork();
    refreshWork({ context: true, initial: true });
  }

  function openWorkForAgenda(index) {
    const item = state.agenda?.items?.[index];
    if (!item) return;
    const task = taskById(item.task);
    if (!task) {
      flash("The task behind this agenda item was not found in TaskStore.", true);
      return;
    }
    if (item.objective !== task.objective) {
      item.objective = task.objective;
      markDirty();
      renderToday();
    }
    openWorkSheet(task, item, index);
  }

  function showWorkById(taskId) {
    const task = taskById(taskId);
    if (!task) {
      flash("That task is not in the current task list. Refresh and try again.", true);
      return;
    }
    const index = state.agenda?.items?.findIndex((item) => item.task === taskId) ?? -1;
    const item = index >= 0 ? state.agenda.items[index] : null;
    if (item && item.objective !== task.objective) {
      item.objective = task.objective;
      markDirty();
      renderToday();
    }
    openWorkSheet(task, item, index, { route: false });
  }

  function startWorkPoll() {
    stopWorkPoll();
    state.workTimer = setInterval(() => {
      if (state.view !== "work") { stopWorkPoll(); return; }
      refreshWork();
    }, 2000);
  }

  function stopWorkPoll() {
    if (state.workTimer) { clearInterval(state.workTimer); state.workTimer = null; }
  }

  async function refreshWorkContext() {
    const [tasks, projects, runs] = await Promise.all([
      api("/api/tasks"),
      api("/api/projects"),
      api("/api/runs"),
    ]);
    state.tasks = tasks.tasks || [];
    state.projects = projects.projects || [];
    state.runs = runs || [];
    renderBuilder();
    if (state.agenda) renderAgendaRows();
    refreshStatus();
  }

  async function refreshWork(options = {}) {
    if (!state.workTask || state.view !== "work" || state.workRefreshing) return;
    state.workRefreshing = true;
    const task = currentWorkTask();
    const previousState = state.workShape?.state || null;
    let changed = Boolean(options.context);
    try {
      let status = null;
      try {
        status = await api(`/api/shapes/${encodeURIComponent(task.project)}`);
      } catch (error) {
        if (error.status !== 404) throw error;
      }
      const nextShape = status?.shape || null;
      const nextQuestions = status?.questions || null;
      if ((nextShape?.state || null) !== previousState) changed = true;
      state.workShape = nextShape;
      state.workQuestions = nextQuestions;
      state.workJobs = [];
      if (nextShape) {
        try {
          const jobs = await api(
            `/api/shapes/${encodeURIComponent(task.project)}/jobs`
          );
          state.workJobs = jobs.jobs || [];
        } catch (error) {
          state.workJobs = [];
        }
      }
      // Render the shape state immediately; task/run context can follow.
      if (state.view === "work") renderWork();
      if (changed || options.context) await refreshWorkContext();
    } catch (error) {
      state.workError = workErrorText(error);
    } finally {
      state.workRefreshing = false;
      if (state.view !== "work") return;
      renderWork();
      const pending = (state.workJobs || []).some((job) =>
        ["queued", "running"].includes(job.status)
      );
      if (
        state.workShape &&
        (pending || ["drafting", "revising"].includes(state.workShape.state))
      ) {
        if (!state.workTimer) startWorkPoll();
      } else {
        stopWorkPoll();
      }
    }
  }

  function renderWork() {
    const task = currentWorkTask();
    if (!task || state.view !== "work") return;
    const project = projectById(task.project);
    const shape = state.workShape;
    const latest = workLatestRun(task.task);
    const active = workActiveRun(task.task);
    const approved = project?.spec_phase === "approved" || shape?.state === "approved";
    $("work-title").textContent = task.title;
    const agendaLabel = state.workAgendaItem
      ? `agenda · ${state.agenda?.status || "draft"}`
      : "not on agenda";
    $("work-meta").innerHTML = `
      <span>${esc(projectName(task.project))}</span>
      <span>${esc(objectiveLabel(task))}</span>
      <span>${esc(task.task)}</span>
      <span>${esc(agendaLabel)}</span>`;
    $("work-stages").innerHTML = renderWorkStages(task, approved, shape, latest, active);
    renderWorkModel();
    const error = $("work-error");
    error.hidden = !state.workError;
    error.textContent = state.workError;
    $("work-shape").innerHTML = renderWorkShape(task, shape, approved);
    $("work-build").innerHTML = renderWorkBuild(task, approved, active, latest);
    $("work-review").innerHTML = renderWorkReview(latest);
  }

  function renderWorkModel() {
    const box = $("work-model-picker");
    if (!box) return;
    const up = buildModels();
    const fallback = up.find((model) => model.id === "flash")?.id || up[0]?.id || "";
    const current = up.some((model) => model.id === state.workModel) ? state.workModel : fallback;
    state.workModel = current;
    box.innerHTML = up.length
      ? modelCards(up, "work_model", current, state.workBusy)
      : '<span class="muted">No model available.</span>';
    const reviewerBox = $("work-reviewer-picker");
    if (reviewerBox) {
      const preferred = up.some((model) => model.id === state.workReviewer)
        ? state.workReviewer
        : state.roles?.reviewer || up[0]?.id || "";
      state.workReviewer = preferred;
      reviewerBox.innerHTML = up.length
        ? modelCards(up, "work_reviewer", preferred, state.workBusy)
        : '<span class="muted">No model available.</span>';
    }
    $("work-model-note").textContent = "";
  }

  function selectedTreeTask() {
    const tree = state.shapeTree?.tree;
    const slug = state.shapeTreeSlug;
    if (!tree || !slug) return null;
    for (const objective of tree.objectives) {
      const task = objective.tasks.find((row) => row.slug === slug);
      if (task) return { objective, task };
    }
    return null;
  }

  function treeEditorHtml(tree, picked) {
    const options = tree.objectives
      .map((objective) => `<option value="${esc(objective.slug)}"${objective.slug === picked.objective.slug ? " selected" : ""}>${esc(objective.title)}</option>`)
      .join("");
    const allTasks = tree.objectives.flatMap((objective) => objective.tasks);
    const deps = allTasks
      .filter((task) => task.slug !== picked.task.slug)
      .map((task) => `<option value="${esc(task.slug)}"${(picked.task.depends_on || []).includes(task.slug) ? " selected" : ""}>${esc(task.title)}</option>`)
      .join("");
    return `
      <div class="dg-editor-grid">
        <label class="sy-field dg-wide"><span>Title</span>
          <input id="tree-title" class="sy-input" value="${esc(picked.task.title)}"></label>
        <label class="sy-field"><span>Objective</span>
          <select id="tree-objective" class="sy-select">${options}</select></label>
        <label class="sy-field"><span>Depends on</span>
          <select id="tree-deps" class="sy-select" multiple size="3">${deps}</select></label>
        <label class="sy-field dg-wide"><span>Gate — one command per line</span>
          <textarea id="tree-gate" class="sy-input" rows="2">${esc((picked.task.gate || []).map((argv) => argv.join(" ")).join("\n"))}</textarea></label>
        <label class="sy-field"><span>Scope — one glob per line</span>
          <textarea id="tree-scope" class="sy-input" rows="2">${esc((picked.task.scope || []).join("\n"))}</textarea></label>
        <label class="sy-field"><span>Acceptance — one per line</span>
          <textarea id="tree-acceptance" class="sy-input" rows="2">${esc((picked.task.acceptance || []).join("\n"))}</textarea></label>
      </div>
      <div class="dg-editor-actions">
        <button class="sy-btn ghost" type="button" data-tree-action="delete">Delete task</button>
        <button class="sy-btn ghost" type="button" data-tree-action="add">Add task to objective</button>
        <button class="sy-btn primary" type="button" data-tree-action="save">Save tree</button>
      </div>`;
  }

  const FULLSCREEN_CARDS = [
    ["shape-tree-card", "shape-tree-expand", "the decomposition"],
    ["shape-spec-card", "shape-spec-expand", "the spec"],
  ];

  function syncFullscreen() {
    let open = false;
    for (const [cardId, buttonId, label] of FULLSCREEN_CARDS) {
      const card = $(cardId);
      const button = $(buttonId);
      if (!card) continue;
      const on = card.classList.contains("fullscreen");
      open = open || on;
      if (!button) continue;
      const expand = button.querySelector(".icon-expand");
      const minimise = button.querySelector(".icon-minimise");
      // SVG elements have no `hidden` IDL property; toggle the attribute.
      if (expand) expand.toggleAttribute("hidden", on);
      if (minimise) minimise.toggleAttribute("hidden", !on);
      button.title = on
        ? `return ${label} to the page`
        : `fill the screen with ${label}`;
      button.setAttribute(
        "aria-label",
        on ? `Minimise ${label}` : `Expand ${label}`
      );
      button.setAttribute("aria-pressed", on ? "true" : "false");
    }
    document.body.classList.toggle("card-fullscreen", open);
  }

  function setCardFullscreen(cardId, on) {
    const card = $(cardId);
    if (!card) return;
    if (on) {
      for (const other of document.querySelectorAll(".sy-card.fullscreen")) {
        if (other !== card) other.classList.remove("fullscreen");
      }
    }
    card.classList.toggle("fullscreen", on);
    syncFullscreen();
  }

  function closeFullscreen() {
    document
      .querySelectorAll(".sy-card.fullscreen")
      .forEach((card) => card.classList.remove("fullscreen"));
    syncFullscreen();
  }

  function setTreeFullscreen(on) {
    setCardFullscreen("shape-tree-card", on);
  }

  function renderShapeTree() {
    const card = $("shape-tree-card");
    const box = $("shape-tree");
    if (!card || !box) return;
    const payload = state.shapeTree;
    const tree = payload?.tree || null;
    card.hidden = !tree;
    const editor = $("shape-tree-editor");
    if (!tree) {
      if (editor) editor.hidden = true;
      setTreeFullscreen(false);
      return;
    }
    const count = tree.objectives.reduce(
      (total, objective) => total + objective.tasks.length,
      0
    );
    $("shape-tree-meta").textContent =
      `${tree.objectives.length} objectives · ${count} tasks · ${payload.state}`
      + (payload.editable ? " · click a task" : " · read-only");
    if (window.renderDraftDiagram) {
      window.renderDraftDiagram(box, tree, {
        selected: state.shapeTreeSlug,
        onSelect: (slug) => {
          state.shapeTreeSlug = state.shapeTreeSlug === slug ? null : slug;
          renderShapeTree();
        },
      });
    }
    const picked = selectedTreeTask();
    if (!editor) return;
    editor.hidden = !(picked && payload.editable);
    if (!editor.hidden) editor.innerHTML = treeEditorHtml(tree, picked);
  }

  function readTreeEditor() {
    const picked = selectedTreeTask();
    if (!picked) return;
    const task = picked.task;
    task.title = $("tree-title").value.trim();
    task.gate = $("tree-gate").value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => line.split(/\s+/));
    task.scope = $("tree-scope").value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    task.acceptance = $("tree-acceptance").value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    task.depends_on = [...$("tree-deps").selectedOptions].map((option) => option.value);
    const targetSlug = $("tree-objective").value;
    if (targetSlug && targetSlug !== picked.objective.slug) {
      const tree = state.shapeTree.tree;
      const target = tree.objectives.find((objective) => objective.slug === targetSlug);
      if (target) {
        picked.objective.tasks = picked.objective.tasks.filter(
          (row) => row.slug !== task.slug
        );
        target.tasks.push(task);
      }
    }
  }

  async function saveShapeTree() {
    if (!state.shapeTree) return;
    try {
      const result = await api(
        `/api/shapes/${encodeURIComponent(state.shapeProject)}/tree`,
        { method: "PUT", body: state.shapeTree.tree }
      );
      state.shapeTree = result;
      if (state.shapeData?.shape) {
        state.shapeData.shape.state = result.state;
        state.shapeData.shape.check = result.check;
      }
      flash(result.check?.ok ? "Tree saved." : "Tree saved; the check has issues.");
      renderShapePage();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function addTreeTask() {
    const tree = state.shapeTree?.tree;
    const picked = selectedTreeTask();
    if (!tree || !picked) return;
    readTreeEditor();
    const existing = new Set(
      tree.objectives.flatMap((objective) => objective.tasks.map((task) => task.slug))
    );
    let index = 1;
    while (existing.has(`task-${index}`)) index += 1;
    const slug = `task-${index}`;
    picked.objective.tasks.push({
      slug,
      title: "New task",
      detail: null,
      acceptance: ["A concrete, testable outcome"],
      gate: [["true"]],
      depends_on: [],
      scope: [],
      taskstore_id: null,
    });
    state.shapeTreeSlug = slug;
    renderShapeTree();
  }

  function deleteTreeTask() {
    const picked = selectedTreeTask();
    if (!picked) return;
    const tree = state.shapeTree.tree;
    picked.objective.tasks = picked.objective.tasks.filter(
      (row) => row.slug !== picked.task.slug
    );
    for (const objective of tree.objectives) {
      for (const task of objective.tasks) {
        task.depends_on = (task.depends_on || []).filter(
          (slug) => slug !== picked.task.slug
        );
      }
      if (!objective.tasks.length) {
        flash("An objective needs at least one task; add one first.", true);
        return;
      }
    }
    state.shapeTreeSlug = null;
    renderShapeTree();
  }

  function renderWorkStages(task, approved, shape, latest, active) {
    const shapeState = approved ? "done" : shape && !["approved", "cancelled"].includes(shape.state) ? "active" : "todo";
    let buildState = "todo";
    let buildText = approved ? (task.ready ? "ready" : "blocked") : "needs shape";
    if (active) { buildState = "active"; buildText = active.state; }
    else if (latest && ["merged"].includes(latest.state)) { buildState = "done"; buildText = "merged"; }
    else if (latest && ["blocked", "error", "cancelled"].includes(latest.state)) { buildState = "stalled"; buildText = latest.state; }
    else if (task.ready) buildState = "ready";
    let reviewState = "todo";
    let reviewText = "pending";
    if (latest?.reviewer_verdict) {
      reviewState = ["approve"].includes(latest.reviewer_verdict) ? "done" : "stalled";
      reviewText = latest.reviewer_verdict;
    } else if (active) {
      reviewState = "active";
      reviewText = "in flight";
    } else if (latest) {
      reviewState = "stalled";
      reviewText = latest.state;
    }
    const stages = [
      ["1", "Shape", shapeState, approved ? "approved" : shape ? shape.state : "not started"],
      ["2", "Build", buildState, buildText],
      ["3", "Review", reviewState, reviewText],
    ];
    return stages.map(([number, label, stateName, detail]) => `
      <div class="work-stage ${stateName}">
        <span class="stage-number">${number}</span>
        <div><b>${label}</b><span>${esc(detail)}</span></div>
      </div>`).join("");
  }

  function renderWorkShape(task, shape, approved) {
    const inquisitor = roleModelLabel("inquisitor");
    const header = `<div class="section-head"><div><h2 class="sy-title">Shape</h2><span class="sy-meta">cc-inquisitor${inquisitor ? ` · ${esc(inquisitor)}` : ""}</span></div><div class="shape-head-actions">${badge(approved ? "approved" : shape?.state || "not started")}<button class="sy-btn ghost" data-work-action="open-shape">Open page</button></div></div>`;
    if (approved) {
      const commit = shape?.approved_commit ? `<p class="muted">Approved at ${esc(shape.approved_commit.slice(0, 12))}.</p>` : "";
      return `${header}<p class="work-copy">The project spec is approved. The gate and task IDs are in the shape branch.</p>${commit}`;
    }
    if (!shape || shape.state === "cancelled") {
      const cancelled = shape?.state === "cancelled" ? '<p class="muted">The previous shape was cancelled.</p>' : "";
      return `${header}
        <p class="work-copy">This item is not shaped yet. Start the inquisitor on <b>${esc(projectName(task.project))}</b>, seeded with <b>${esc(objectiveLabel(task))}</b>. Existing TaskStore IDs are adopted by title.</p>
        ${cancelled}
        <div class="work-actions"><button class="sy-btn primary" data-work-action="start-shape" ${state.workBusy ? "disabled" : ""}>Start shape</button></div>`;
    }
    if (["drafting", "revising"].includes(shape.state)) {
      return `${header}<p class="work-running">Inquisitor is working. This can take a few minutes.</p><div class="work-actions"><button class="sy-btn ghost" data-work-action="cancel-shape">Cancel shape</button></div>`;
    }
    if (shape.state === "error" || shape.state === "check_failed") {
      const issues = shape.check?.issues || [];
      const details = shape.error || issues.join("\n") || "The shape check failed.";
      return `${header}<pre class="work-error-block">${esc(details)}</pre>
        <div class="work-actions">
          <button class="sy-btn ghost" data-work-action="shape-diff">View diff</button>
          <button class="sy-btn ghost" data-work-action="cancel-shape">Cancel shape</button>
        </div>
        ${state.workDiff ? `<pre class="pre work-diff">${esc(state.workDiff)}</pre>` : ""}`;
    }
    if (shape.state === "questions" || shape.state === "answers_ready") {
      const questions = state.workQuestions?.questions || [];
      const pending = (state.workJobs || []).find((job) =>
        ["queued", "running"].includes(job.status)
      );
      if (pending) {
        return `${header}<p class="work-running">${esc(pending.title)} is ${esc(pending.status)}. The shape page shows the session and its queue.</p>
          <div class="work-actions">
            <button class="sy-btn ghost" data-work-action="shape-diff">View diff</button>
            <button class="sy-btn ghost" data-work-action="open-shape">Open page</button>
          </div>`;
      }
      const rows = questions.map((question) => `
        <div class="work-question">
          <label for="work-q-${esc(question.id)}">${esc(question.question)}</label>
          <input id="work-q-${esc(question.id)}" class="sy-input" data-work-question="${esc(question.id)}" value="${esc(question.answer || "")}" placeholder="${esc(question.default || "")}">
          ${question.default ? `<span class="sy-meta">default: ${esc(question.default)}</span>` : ""}
        </div>`).join("");
      const unanswered = questions.filter((question) => !(question.answer || "").trim());
      const action = unanswered.length
        ? `<button class="sy-btn primary" data-work-action="answer-revise" ${state.workBusy ? "disabled" : ""}>Save answers & revise</button>`
        : `<button class="sy-btn primary" data-work-action="revise-shape" ${state.workBusy ? "disabled" : ""}>Revise shape</button>`;
      const defaults = unanswered.length
        ? `<button class="sy-btn ghost" data-work-action="fill-defaults">Use defaults</button>`
        : "";
      return `${header}<p class="work-copy">The inquisitor has decisions for you. Defaults are proposals, not answers.</p>
        <div class="work-questions">${rows}</div>
        <div class="work-actions">${action}${defaults}<button class="sy-btn ghost" data-work-action="shape-diff">View diff</button><button class="sy-btn ghost" data-work-action="cancel-shape">Cancel</button></div>
        ${state.workDiff ? `<pre class="pre work-diff">${esc(state.workDiff)}</pre>` : ""}`;
    }
    if (shape.state === "ready") {
      return `${header}<p class="work-copy">The shape passes the pristine check. Materialize it to assign TaskStore ids and generate the gate file, then review the final diff before approving.</p>
        <div class="work-actions">
          <button class="sy-btn secondary" data-work-action="shape-diff">View diff</button>
          <button class="sy-btn primary" data-work-action="materialize-shape" ${state.workBusy ? "disabled" : ""}>Materialize shape</button>
          <button class="sy-btn ghost" data-work-action="cancel-shape">Cancel</button>
        </div>
        ${state.workDiff ? `<pre class="pre work-diff">${esc(state.workDiff)}</pre>` : ""}`;
    }
    if (["materialized", "approving"].includes(shape.state)) {
      return `${header}<p class="work-copy">Materialized. Review the final diff, then approve it to merge the shape branch and approve the spec.</p>
        <div class="work-actions">
          <button class="sy-btn secondary" data-work-action="shape-diff">View diff</button>
          <button class="sy-btn primary" data-work-action="approve-shape" ${state.workBusy ? "disabled" : ""}>Approve shape</button>
          <button class="sy-btn ghost" data-work-action="cancel-shape">Cancel</button>
        </div>
        ${state.workDiff ? `<pre class="pre work-diff">${esc(state.workDiff)}</pre>` : ""}`;
    }
    return `${header}<p class="work-copy">Shape state: ${esc(shape.state)}.</p>`;
  }

  function renderWorkBuild(task, approved, active, latest) {
    const buildBadge = active ? active.state : latest?.state === "merged" && !task.ready ? "merged" : !approved ? "needs shape" : task.ready ? "ready" : "blocked";
    const header = `<div class="section-head"><div><h2 class="sy-title">Build</h2><span class="sy-meta">coder → gate → policy</span></div>${badge(buildBadge)}</div>`;
    if (active) {
      return `${header}<p class="work-copy">Run ${esc(active.id)} is ${esc(active.state)}.</p>
        <div class="work-actions"><button class="sy-btn primary" data-work-action="open-run" data-run-id="${esc(active.id)}">Open run</button></div>`;
    }
    if (latest?.state === "merged" && !task.ready) {
      return `${header}<p class="work-copy">This task is complete. The latest run merged into integration.</p>
        <div class="work-actions"><button class="sy-btn ghost" data-work-action="open-run" data-run-id="${esc(latest.id)}">Open run</button></div>`;
    }
    if (!approved) {
      return `${header}<p class="work-copy">Approve the shape before building. The task is not dispatchable yet.</p>`;
    }
    const reasons = task.reasons || [];
    const reasonHtml = reasons.length
      ? `<div class="reason-list">${reasons.map((reason) => `<div class="reason-line">${esc(reason.code)} — ${esc(reason.detail)}</div>`).join("")}</div>`
      : "";
    if (!task.ready) {
      const hint = reasons.some((reason) => reason.code === "no_gate")
        ? '<p class="muted">Shape approved, but this task still has no gate. It may not have been adopted by the new decomposition.</p>'
        : "";
      return `${header}${reasonHtml}${hint}`;
    }
    const model = buildModels().find((item) => item.id === state.workModel) || null;
    const agendaDraft = state.workAgendaItem && state.agenda?.status === "draft";
    const label = agendaDraft ? "Lock agenda & build" : "Build task";
    return `${header}<p class="work-copy">${latest && latest.state === "merged" ? "A previous build merged. You can run the next attempt." : "Start the coder. The reviewer runs automatically."}</p>
      <p class="work-copy">Build model: <b>${model ? esc(model.label) : "none available"}</b>${model ? ` <span class="muted">(${esc(model.reason)})</span>` : ""}</p>
      <div class="work-actions"><button class="sy-btn primary" data-work-action="build" ${model && !state.workBusy ? "" : "disabled"}>${label}</button></div>`;
  }

  function renderWorkReview(latest) {
    const reviewer = roleModelLabel("reviewer");
    const header = `<div class="section-head"><div><h2 class="sy-title">Review</h2><span class="sy-meta">independent reviewer${reviewer ? ` · ${esc(reviewer)}` : ""}</span></div>${latest ? badge(latest.reviewer_verdict || latest.state) : badge("pending")}</div>`;
    if (!latest) {
      return `${header}<p class="work-copy">No build yet. Review starts inside the run after the gate and policy checks.</p>`;
    }
    const notes = (latest.notes || []).slice(-2).map((note) => `<div class="notes">${esc(note)}</div>`).join("");
    return `${header}<div class="run-meta"><div>run ${esc(latest.id)} · ${esc(latest.state)}</div><div>reviewer verdict: ${esc(latest.reviewer_verdict || "pending")}</div>${notes}</div><div class="work-actions"><button class="sy-btn ghost" data-work-action="open-run" data-run-id="${esc(latest.id)}">Open run</button></div>`;
  }

  async function startShape() {
    const task = currentWorkTask();
    if (!task) return;
    if (!confirm(`Shape ${projectName(task.project)} with ${task.objective} as the priority?`)) return;
    state.workError = "";
    state.workBusy = true;
    renderWork();
    try {
      const shape = await api("/api/shapes", {
        method: "POST",
        body: {
          project: task.project,
          objective: task.objective,
          task: task.task,
          rewrite: false,
        },
      });
      state.workShape = shape;
      state.workQuestions = null;
      state.workDiff = "";
      startWorkPoll();
      renderWork();
      flash("Shape started.");
    } catch (error) {
      state.workError = workErrorText(error);
    } finally {
      state.workBusy = false;
      if (state.view === "work") renderWork();
    }
  }

  async function answerAndRevise() {
    const task = currentWorkTask();
    if (!task) return;
    const answers = {};
    for (const input of document.querySelectorAll("[data-work-question]")) {
      const value = input.value.trim();
      if (value) answers[input.dataset.workQuestion] = value;
    }
    if (!Object.keys(answers).length) {
      showWorkError("Type at least one answer.");
      return;
    }
    state.workError = "";
    state.workBusy = true;
    renderWork();
    try {
      await api(`/api/shapes/${encodeURIComponent(task.project)}/jobs`, {
        method: "POST",
        body: { kind: "answers", answers },
      });
      state.workQuestions = null;
      flash("Answers queued. The inquisitor is revising.");
      startWorkPoll();
      await refreshWork({ context: true });
    } catch (error) {
      state.workError = workErrorText(error);
      await refreshWork({ context: true });
    } finally {
      state.workBusy = false;
      if (state.view === "work") renderWork();
    }
  }

  async function reviseShape() {
    const task = currentWorkTask();
    if (!task) return;
    state.workError = "";
    state.workBusy = true;
    renderWork();
    try {
      await api(`/api/shapes/${encodeURIComponent(task.project)}/jobs`, {
        method: "POST",
        body: { kind: "revise" },
      });
      flash("Revision queued.");
      startWorkPoll();
      await refreshWork({ context: true });
    } catch (error) {
      state.workError = workErrorText(error);
      await refreshWork({ context: true });
    } finally {
      state.workBusy = false;
      if (state.view === "work") renderWork();
    }
  }

  async function materializeShape() {
    const task = currentWorkTask();
    if (!task) return;
    state.workError = "";
    state.workBusy = true;
    renderWork();
    try {
      const shape = await api(`/api/shapes/${encodeURIComponent(task.project)}/materialize`, { method: "POST" });
      state.workShape = shape;
      state.workDiff = "";
      await refreshWorkContext();
      renderWork();
      flash("Shape materialized. Review the diff before approval.");
    } catch (error) {
      state.workError = workErrorText(error);
    } finally {
      state.workBusy = false;
      if (state.view === "work") renderWork();
    }
  }

  async function approveShape() {
    const task = currentWorkTask();
    if (!task) return;
    if (!confirm("Approve this shape? It materializes tasks and approves the project spec.")) return;
    state.workError = "";
    state.workBusy = true;
    renderWork();
    try {
      const shape = await api(`/api/shapes/${encodeURIComponent(task.project)}/approve`, { method: "POST" });
      state.workShape = shape;
      state.workDiff = "";
      stopWorkPoll();
      await refreshWorkContext();
      renderWork();
      flash("Shape approved. This task is now eligible if it was preserved.");
    } catch (error) {
      state.workError = workErrorText(error);
    } finally {
      state.workBusy = false;
      if (state.view === "work") renderWork();
    }
  }

  async function cancelShape() {
    const task = currentWorkTask();
    if (!task || !confirm("Cancel the active shape?")) return;
    try {
      state.workShape = await api(`/api/shapes/${encodeURIComponent(task.project)}/cancel`, { method: "POST" });
      state.workError = "";
      state.workDiff = "";
      stopWorkPoll();
      await refreshWorkContext();
      renderWork();
      flash("Shape cancelled.");
    } catch (error) {
      state.workError = workErrorText(error);
      renderWork();
    }
  }

  // ------------------------------------------------------------- shape page
  function openShapePage(project, seed = null) {
    if (state.shapeProject !== project) {
      state.shapeData = null;
      state.shapeTree = null;
      state.shapeTreeSlug = null;
      state.shapeDiff = "";
      state.shapeError = "";
      state.specDocs = null;
      state.useCases = null;
      state.c4 = null;
      state.specTab = "md";
      state.useStep = null;
      state.useDirty = false;
      state.c4Element = null;
      state.c4Dirty = false;
      state.expanded = [];
      state.activeUseCase = null;
      state.specKey = null;
    }
    state.shapeProject = project;
    if (seed) state.shapeSeed = seed;
    const target = `shape/${encodeURIComponent(project)}`;
    if (location.hash.replace(/^#/, "") !== target) {
      location.hash = target;
      return;
    }
    showView("shape");
    loadShapePage();
  }

  async function loadShapePage() {
    const project = state.shapeProject;
    if (!project) return;
    try {
      let status = null;
      try {
        status = await api(`/api/shapes/${encodeURIComponent(project)}`);
      } catch (error) {
        if (error.status !== 404) throw error;
      }
      state.shapeData = status;
      state.shapeTree = null;
      const shape = status?.shape;
      state.shapeJobs = [];
      if (shape) {
        try {
          const data = await api(
            `/api/shapes/${encodeURIComponent(project)}/jobs`
          );
          state.shapeJobs = data.jobs || [];
        } catch (error) {
          state.shapeJobs = [];
        }
      }
      if (shape && !["approved", "cancelled"].includes(shape.state)) {
        try {
          state.shapeTree = await api(`/api/shapes/${encodeURIComponent(project)}/tree`);
        } catch (error) {
          state.shapeTree = null;
        }
      }
      const specKey = shape ? `${shape.id}:${shape.updated}` : null;
      if (shape && specKey !== state.specKey && !state.useDirty) {
        state.specKey = specKey;
        try {
          state.specDocs = await api(
            `/api/shapes/${encodeURIComponent(project)}/spec`
          );
        } catch (error) {
          state.specDocs = null;
        }
        try {
          state.useCases = await api(
            `/api/shapes/${encodeURIComponent(project)}/use-cases`
          );
        } catch (error) {
          state.useCases = null;
        }
        if (!state.c4Dirty) {
          try {
            state.c4 = await api(
              `/api/shapes/${encodeURIComponent(project)}/c4`
            );
          } catch (error) {
            state.c4 = null;
          }
        }
      }
      if (!shape) {
        state.specDocs = null;
        state.useCases = null;
        state.c4 = null;
        state.useStep = null;
        state.c4Element = null;
        state.expanded = [];
        state.activeUseCase = null;
        state.specKey = null;
        state.useDirty = false;
        state.c4Dirty = false;
      }
      state.shapeError = "";
    } catch (error) {
      state.shapeData = null;
      state.shapeTree = null;
      state.shapeError = workErrorText(error);
    }
    if (state.view !== "shape") return;
    renderShapePage();
    if (state.shapeTimer) {
      clearInterval(state.shapeTimer);
      state.shapeTimer = null;
    }
    const shapeState = state.shapeData?.shape?.state;
    const pending = state.shapeJobs.some((job) =>
      ["queued", "running"].includes(job.status)
    );
    if (pending || (shapeState && ["drafting", "revising"].includes(shapeState))) {
      state.shapeTimer = setInterval(() => {
        if (state.view === "shape") loadShapePage();
      }, 2500);
    }
  }

  function shapeCard(title, meta, body, actions = "") {
    return `<section class="sy-card work-section">
      <div class="section-head">
        <div><h2 class="sy-title">${esc(title)}</h2><span class="sy-meta">${esc(meta)}</span></div>
      </div>
      ${body}
      ${actions ? `<div class="work-actions">${actions}</div>` : ""}
    </section>`;
  }

  function shapeStartHtml(project) {
    const row = projectById(project);
    const approved = row?.spec_phase === "approved";
    if (approved) {
      return shapeCard(
        "Spec approved",
        "this project has a canonical spec",
        '<p class="work-copy">The spec is approved and its tasks are materialized in TaskStore. Start a shape only to revise it.</p>',
        '<button class="sy-btn ghost" data-shape-action="start">Shape a revision</button>'
      );
    }
    return shapeCard(
      "Not shaped yet",
      "no active draft",
      `<p class="work-copy">Start the inquisitor on <b>${esc(projectName(project))}</b>. It audits the repo and existing docs, then asks only about gaps.</p>`,
      '<button class="sy-btn primary" data-shape-action="start">Start shaping</button>'
    );
  }

  function shapeTurnPending() {
    return (state.shapeJobs || []).some((job) =>
      ["queued", "running"].includes(job.status)
    );
  }

  function queuedAnswerIds() {
    const ids = new Set();
    for (const job of state.shapeJobs || []) {
      if (!["queued", "running"].includes(job.status)) continue;
      for (const row of job.payload?.answers || []) ids.add(row.id);
    }
    return ids;
  }

  function openShapeQuestions(questions) {
    const queued = queuedAnswerIds();
    return questions.filter(
      (question) =>
        !(question.answer || "").trim() && !queued.has(question.id)
    );
  }

  function shapeQuestionsHtml(questions) {
    const open = openShapeQuestions(questions);
    if (!open.length) {
      return shapeTurnPending()
        ? '<p class="work-copy">Every question is sent. The inquisitor is folding the answers in and will ask again if it needs to.</p>'
        : '<p class="work-copy">Every question is answered. Run the check, or revise from the composer above.</p>';
    }
    return `<div class="shape-questions">${open
      .map(
        (question) => `<label class="sy-field shape-question">
          <span>${esc(question.question)}</span>
          <textarea class="sy-input" rows="1" data-shape-q="${esc(question.id)}"
            placeholder="${esc(question.default ? `default: ${question.default}` : "Type your answer")}"></textarea>
        </label>`
      )
      .join("")}</div>
      <p class="sy-meta">Enter sends · Shift+Enter for a new line</p>`;
  }

  function growField(field) {
    if (!field || field.tagName !== "TEXTAREA") return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight + 2, 180)}px`;
  }

  function updateAnswerAction() {
    const fields = [...document.querySelectorAll("#shape-body [data-shape-q]")];
    const filled = fields.filter((field) => field.value.trim()).length;
    const button = document.querySelector(
      '#shape-body [data-shape-action="answer-revise"]'
    );
    if (!button) return;
    button.disabled = !filled;
    button.textContent =
      filled > 1 ? `Answer ${filled} & revise` : "Answer & revise";
    button.title = filled
      ? `send ${filled} answer${filled === 1 ? "" : "s"} as one turn`
      : "type an answer first";
  }

  async function queueAnswers(answers, focusIndex = 0) {
    const project = state.shapeProject;
    const count = Object.keys(answers).length;
    if (!project || !count) return;
    try {
      await api(`/api/shapes/${encodeURIComponent(project)}/jobs`, {
        method: "POST",
        body: { kind: "answers", answers },
      });
      flash(count > 1 ? `${count} answers queued.` : "Answer queued.");
    } catch (error) {
      flash(error.message, true);
      return;
    }
    await loadShapePage();
    const fields = [...document.querySelectorAll("#shape-body [data-shape-q]")];
    if (fields.length) fields[Math.min(focusIndex, fields.length - 1)].focus();
    updateAnswerAction();
  }

  function collectAnswers() {
    const answers = {};
    for (const field of document.querySelectorAll("#shape-body [data-shape-q]")) {
      const value = field.value.trim();
      if (value) answers[field.dataset.shapeQ] = value;
    }
    return answers;
  }

  function shapeBodyHtml(shape, questions) {
    const stateName = shape.state;
    if (["drafting", "revising"].includes(stateName)) {
      return shapeCard(
        stateName === "drafting" ? "Inquisitor drafting" : "Inquisitor revising",
        "this can take a few minutes",
        '<p class="work-copy">The page refreshes itself when the pass finishes.</p>',
        ''
      );
    }
    if (stateName === "questions" || stateName === "answers_ready") {
      const open = openShapeQuestions(questions).length;
      const unsent = questions.filter((q) => !(q.answer || "").trim()).length;
      const queued = unsent - open;
      const meta = [open ? `${open} open` : "none open"];
      if (queued > 0) meta.push(`${queued} in the queue`);
      return shapeCard(
        "Questions",
        meta.join(" · "),
        shapeQuestionsHtml(questions),
        '<button class="sy-btn primary" data-shape-action="answer-revise" disabled>Answer & revise</button>'
      );
    }
    if (stateName === "ready") {
      return shapeCard(
        "Check passes",
        "materialize the decomposition into TaskStore",
        '<p class="work-copy">The check passes. Approve materializes the decomposition into TaskStore, writes the gate file and merges the spec.</p>'
      );
    }
    if (stateName === "materialized") {
      return shapeCard(
        "Ready to approve",
        "TaskStore ids exist",
        '<p class="work-copy">TaskStore ids exist. Approve merges the shape into <code>cc/integration</code> and records the spec digest.</p>'
      );
    }
    if (stateName === "error") {
      return shapeCard(
        "Inquisitor failed",
        "the session is kept, so a revision resumes it",
        `<p class="work-copy">${esc(shape.error || "the pass failed")}</p>`,
        '<button class="sy-btn primary" data-shape-action="revise">Resume & revise</button>'
      );
    }
    if (stateName === "check_failed") {
      const issues = (shape.check?.issues || [])
        .slice(0, 8)
        .map((issue) => `<div class="reason-line">${esc(issue)}</div>`)
        .join("");
      return shapeCard(
        "Check failed",
        "the inquisitor can repair this in its own session",
        `${issues || '<p class="work-copy">the check failed</p>'}`,
        '<button class="sy-btn primary" data-shape-action="revise">Fix & revise</button>'
      );
    }
    return shapeCard(
      `Shape ${stateName}`,
      "",
      '<p class="work-copy">Nothing to do here.</p>',
      '<button class="sy-btn ghost" data-shape-action="refresh">Refresh</button>'
    );
  }

  function jobElapsed(job) {
    if (!job.started) return "";
    const started = new Date(job.started).getTime();
    if (!started) return "";
    const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
    const minutes = Math.floor(seconds / 60);
    return minutes ? `${minutes}:${String(seconds % 60).padStart(2, "0")}` : `${seconds}s`;
  }

  function renderShapeSession() {
    const card = $("shape-session");
    if (!card) return;
    const shape = state.shapeData?.shape || null;
    card.hidden = !shape;
    const divider = $("shape-divider");
    if (divider) divider.hidden = !shape;
    if (!shape) return;
    const jobs = state.shapeJobs || [];
    const done = jobs.filter((job) => ["done", "failed"].includes(job.status));
    const running = jobs.find((job) => job.status === "running");
    const queued = jobs.filter((job) => job.status === "queued").length;
    const tokensIn = jobs.reduce((sum, job) => sum + (job.tokens_in || 0), 0);
    const tokensOut = jobs.reduce((sum, job) => sum + (job.tokens_out || 0), 0);
    const cost = jobs.reduce((sum, job) => sum + (job.cost || 0), 0);
    const lastContext = [...jobs].reverse().find((job) => job.context)?.context || 0;
    const pack = (value) => {
      if (value < 1024) return String(value);
      if (value >= 1024 * 1024) {
        const millions = value / (1024 * 1024);
        return Number.isInteger(millions) ? `${millions}M` : `${millions.toFixed(1)}M`;
      }
      const thousands = value / 1024;
      return thousands >= 100
        ? `${Math.round(thousands)}k`
        : `${thousands.toFixed(1)}k`;
    };
    const inquisitorId = state.roles?.inquisitor;
    const windowSize =
      state.models.find((model) => model.id === inquisitorId)?.context_window || 0;
    const usedPct = windowSize && lastContext
      ? Math.round((lastContext / windowSize) * 100)
      : 0;
    const contextText = lastContext
      ? windowSize
        ? `context ${pack(lastContext)}/${pack(windowSize)} · ${usedPct}%`
        : `context ${pack(lastContext)}`
      : "";
    const contextClass = usedPct >= 90 ? " bad" : usedPct >= 75 ? " warn" : "";
    const failed = jobs.filter((job) => job.status === "failed").length;
    const sessionParts = [
      `${pack(tokensIn)} in / ${pack(tokensOut)} out`,
      `$${cost.toFixed(3)}`,
    ];
    const sessionHtml = sessionParts
      .map((part) => `<span class="vital">${part}</span>`)
      .join("");
    const contextHtml = contextText
      ? `<span class="vital${contextClass}">${contextText}</span>`
      : "";
    const queueParts = [
      running
        ? `running ${esc(running.title)} <b id="shape-running-timer">${jobElapsed(running)}</b>`
        : "",
      queued ? `${queued} queued` : "queue empty",
      `${done.length} turn${done.length === 1 ? "" : "s"}`,
      failed ? `${failed} failed` : "",
    ].filter(Boolean);
    $("shape-vitals").innerHTML = `
      <div class="vitals-left">${sessionHtml}${contextHtml}</div>
      <div class="vitals-right">${queueParts
        .map((part) => `<span class="vital">${part}</span>`)
        .join("")}</div>`;
    if (running) startShapeClock();
    else stopShapeClock();

    const chat = $("shape-chat");
    const atBottom =
      chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 40;
    chat.innerHTML = done.length
      ? done
          .map((job) => {
            const body =
              job.status === "failed"
                ? `<div class="job-error">${esc(job.error || "failed")}</div>`
                : `<div class="job-reply">${esc(job.reply || "no reply text")}</div>`;
            return `<div class="job-block"><div class="job-delim">___${esc(job.title)}___</div>${body}</div>`;
          })
          .join("")
      : '<p class="muted">No turns yet. Discuss a question, or instruct a revision.</p>';
    if (atBottom || !chat.dataset.ready) {
      chat.scrollTop = chat.scrollHeight;
      chat.dataset.ready = "1";
    }
    document.querySelectorAll("#shape-mode button").forEach((button) => {
      button.classList.toggle("active", button.dataset.shapeMode === state.shapeMode);
    });
    $("shape-send").disabled = false;
  }

  function startShapeClock() {
    if (state.shapeClock) return;
    state.shapeClock = setInterval(() => {
      const running = (state.shapeJobs || []).find(
        (job) => job.status === "running"
      );
      const cell = $("shape-running-timer");
      if (!running || !cell) return;
      cell.textContent = jobElapsed(running);
    }, 1000);
  }

  function stopShapeClock() {
    if (!state.shapeClock) return;
    clearInterval(state.shapeClock);
    state.shapeClock = null;
  }

  async function sendShapeMessage() {
    const project = state.shapeProject;
    const text = $("shape-prompt").value.trim();
    if (!project) return;
    if (!text) {
      flash("Type a message first.", true);
      return;
    }
    const kind = state.shapeMode === "revise" ? "revise" : "discuss";
    try {
      await api(`/api/shapes/${encodeURIComponent(project)}/jobs`, {
        method: "POST",
        body: { kind, message: text },
      });
      $("shape-prompt").value = "";
      flash(
        kind === "revise"
          ? "Revision queued."
          : "Message queued."
      );
      await loadShapePage();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function renderShapeModel() {
    const box = $("shape-model-picker");
    if (!box) return;
    const models = buildModels();
    const current = state.shapeData?.shape?.model || state.roles?.inquisitor || "";
    box.innerHTML = models.length
      ? modelCards(models, "shape_model", current, false)
      : '<span class="muted">No model available.</span>';
  }

  function specFiles() {
    return state.specDocs?.files || [];
  }

  function useCaseDoc() {
    if (!state.useCases) return null;
    if (!state.useCases.use_cases) {
      state.useCases.use_cases = { version: 1, use_cases: [] };
    }
    return state.useCases.use_cases;
  }

  function specUseCases() {
    return useCaseDoc()?.use_cases || [];
  }

  function c4Doc() {
    return state.c4?.c4 || null;
  }

  function c4Level(key) {
    const doc = c4Doc();
    if (!doc) return { elements: [], relationships: [] };
    if (!doc.levels[key]) doc.levels[key] = { elements: [], relationships: [] };
    return doc.levels[key];
  }

  const C4_LEVELS = [
    ["context", "Context"],
    ["container", "Container"],
    ["component", "Component"],
    ["code", "Code"],
  ];
  const C4_TYPES = [
    "person",
    "system",
    "external",
    "container",
    "component",
    "database",
    "queue",
    "code",
    "decision",
  ];
  const C4_DEFAULT_TYPE = {
    context: "system",
    container: "container",
    component: "component",
    code: "code",
  };

  function renderShapeSpec() {
    const card = $("shape-spec-card");
    if (!card) return;
    const shape = state.shapeData?.shape || null;
    const files = specFiles();
    const cases = specUseCases();
    const c4 = c4Doc();
    const editable = Boolean(state.useCases?.editable ?? state.c4?.editable);
    const visible = Boolean(shape) && (files.length > 0 || cases.length > 0 || Boolean(c4));
    card.hidden = !visible;
    if (!visible) {
      if (card.classList.contains("fullscreen")) {
        setCardFullscreen("shape-spec-card", false);
      }
      $("shape-use-editor").hidden = true;
      return;
    }

    const tabs = [{ id: "md", label: "Markdown" }];
    if (cases.length) tabs.push({ id: "usecases", label: "Use cases" });
    if (c4) {
      for (const [id, label] of C4_LEVELS) tabs.push({ id, label });
    }
    if (!tabs.some((tab) => tab.id === state.specTab)) state.specTab = "md";

    const counts = [
      files.length ? `${files.length} doc${files.length === 1 ? "" : "s"}` : "",
      cases.length
        ? `${cases.length} use case${cases.length === 1 ? "" : "s"}`
        : "",
      c4
        ? `${C4_LEVELS.reduce(
            (sum, [key]) => sum + c4Level(key).elements.length,
            0
          )} C4 elements`
        : "",
    ].filter(Boolean);
    $("shape-spec-meta").textContent = counts.join(" · ");
    $("shape-spec-tabs").innerHTML =
      tabs
        .map(
          (tab) =>
            `<button type="button" role="tab" class="spec-tab${
              tab.id === state.specTab ? " active" : ""
            }" data-spec-tab="${esc(tab.id)}" aria-selected="${
              tab.id === state.specTab
            }">${esc(tab.label)}</button>`
        )
        .join("") +
      (editable
        ? '<button type="button" class="spec-tab spec-add" data-spec-action="add-use-case" title="add a use case" aria-label="Add a use case">+</button>'
        : "");

    const extrapolate = $("shape-spec-generate");
    if (extrapolate) {
      extrapolate.hidden = !editable;
      extrapolate.disabled = shapeTurnPending();
      extrapolate.title = cases.length || c4
        ? "update the use cases and the C4 model from the spec — two turns, keeps your edits"
        : "derive the use cases and the C4 model from the spec — two turns";
    }

    if (state.specTab === "md") {
      $("shape-spec-body").innerHTML = files.length
        ? `<div class="spec-docs">${files
            .map(
              (file) =>
                `<article class="spec-doc"><div class="spec-doc-name">${esc(
                  file.path
                )}</div>${
                  window.renderMarkdown
                    ? window.renderMarkdown(file.text)
                    : `<pre>${esc(file.text)}</pre>`
                }</article>`
            )
            .join("")}</div>`
        : '<p class="muted">No spec document in the worktree yet.</p>';
      $("shape-use-editor").hidden = true;
      return;
    }

    if (state.specTab === "usecases") {
      renderUseCasesTab(cases, editable);
      return;
    }

    renderC4Tab(state.specTab, Boolean(state.c4?.editable));
  }

  function renderUseCasesTab(cases, editable) {
    const collapsed = new Set(
      cases
        .map((useCase) => useCase.id)
        .filter((id) => !state.expanded.includes(id))
    );
    $("shape-spec-body").innerHTML =
      '<div class="dg-wrap"><div id="shape-flow"></div></div>';
    if (window.renderUseCaseDiagram) {
      window.renderUseCaseDiagram($("shape-flow"), { use_cases: cases }, {
        tasks: state.shapeTree?.tree || null,
        selected: state.useStep,
        collapsed,
        onToggle: (id) => {
          const at = state.expanded.indexOf(id);
          if (at >= 0) state.expanded.splice(at, 1);
          else {
            state.expanded.push(id);
            state.activeUseCase = id;
          }
          renderShapeSpec();
        },
        onSelect: (uc, index) => {
          if (!state.expanded.includes(uc)) state.expanded.push(uc);
          state.activeUseCase = uc;
          state.useStep = { uc, index };
          renderShapeSpec();
        },
      });
    }
    const editing =
      editable &&
      state.activeUseCase &&
      state.expanded.includes(state.activeUseCase);
    $("shape-use-editor").hidden = !editing;
    if (editing) renderUseEditor(state.activeUseCase);
  }

  function renderC4Tab(levelKey, editable) {
    const level = c4Level(levelKey);
    $("shape-spec-body").innerHTML =
      '<div class="dg-wrap"><div id="shape-c4"></div></div>';
    if (window.renderC4Diagram) {
      window.renderC4Diagram($("shape-c4"), level, {
        selected: state.c4Element,
        onSelect: (id) => {
          state.c4Element = id;
          renderShapeSpec();
        },
      });
    }
    $("shape-use-editor").hidden = !editable;
    if (editable) renderC4Editor(levelKey);
  }

  function ensureStepIds(useCase) {
    const used = new Set(useCase.steps.map((step) => step.id).filter(Boolean));
    let counter = 0;
    for (const step of useCase.steps) {
      if (step.id) continue;
      do {
        counter += 1;
      } while (used.has(`s${counter}`));
      step.id = `s${counter}`;
      used.add(step.id);
    }
  }

  function branchesValid(useCase) {
    const positions = new Map();
    useCase.steps.forEach((step, index) => {
      if (step.id) positions.set(step.id, index);
    });
    return useCase.steps.every((step, index) =>
      (step.branches || []).every((branch) => {
        const target = positions.get(branch.to);
        return target != null && target > index;
      })
    );
  }

  function readUseEditor() {
    const useCase = specUseCases().find(
      (row) => row.id === state.activeUseCase
    );
    if (!useCase) return;
    if ($("uc-title")) useCase.title = $("uc-title").value.trim() || useCase.title;
    if ($("uc-actor")) useCase.actor = $("uc-actor").value.trim() || null;
    if ($("uc-summary")) useCase.summary = $("uc-summary").value.trim() || null;
    const picked =
      state.useStep && state.useStep.uc === useCase.id ? state.useStep.index : null;
    const step = picked != null ? useCase.steps[picked] : null;
    if (!step) return;
    if ($("step-label")) step.label = $("step-label").value.trim() || step.label;
    if ($("step-task")) step.task = $("step-task").value || null;
    if ($("step-kind")) step.kind = $("step-kind").value === "choice" ? "choice" : "activity";
    if (step.kind === "choice") {
      const guards = [...$("shape-use-editor").querySelectorAll("[data-branch-guard]")];
      const targets = [...$("shape-use-editor").querySelectorAll("[data-branch-to]")];
      step.branches = guards
        .map((input, index) => ({
          guard: input.value.trim() || "yes",
          to: targets[index]?.value || "",
        }))
        .filter((branch) => branch.to);
    } else {
      step.branches = [];
    }
  }

  function branchRowsHtml(useCase, picked, step) {
    const later = useCase.steps.slice(picked + 1).filter((row) => row.id);
    if (!later.length) {
      return '<p class="muted dg-wide">A branch needs a later step to point at. Add one below.</p>';
    }
    const rows = (step.branches || [])
      .map((branch, index) => {
        const options = later
          .map(
            (row) =>
              `<option value="${esc(row.id)}"${
                row.id === branch.to ? " selected" : ""
              }>${esc(row.label)}</option>`
          )
          .join("");
        return `<div class="uc-branch-row">
          <input class="sy-input" data-branch-guard="${index}" value="${esc(
            branch.guard
          )}" aria-label="Guard">
          <select class="sy-select" data-branch-to="${index}" aria-label="Target step">${options}</select>
          <button class="sy-btn ghost" type="button" data-use-action="remove-branch" data-branch="${index}" title="remove this branch">×</button>
        </div>`;
      })
      .join("");
    return `<div class="dg-wide uc-branches">
      <div class="uc-branch-head"><span>Choice branches</span><span>${later.length} later step${later.length === 1 ? "" : "s"}</span></div>
      ${rows}
      <button class="sy-btn ghost" type="button" data-use-action="add-branch" ${
        (step.branches || []).length >= 4 ? "disabled" : ""
      }>Add branch</button>
    </div>`;
  }

  function renderUseEditor(ucId) {
    const box = $("shape-use-editor");
    const useCase = specUseCases().find((row) => row.id === ucId);
    if (!useCase) {
      box.hidden = true;
      return;
    }
    const picked =
      state.useStep && state.useStep.uc === ucId ? state.useStep.index : null;
    const step = picked != null ? useCase.steps[picked] : null;
    const tasks = (state.shapeTree?.tree?.objectives || []).flatMap(
      (objective) => objective.tasks
    );
    // The page polls while a turn runs. Rebuilding an editor the operator is
    // typing in would wipe the field, so only rebuild when the model changed.
    const signature = JSON.stringify(["use", ucId, state.useStep, useCase]);
    if (!box.hidden && box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    const options = [
      '<option value="">— no task —</option>',
      ...tasks.map(
        (task) =>
          `<option value="${esc(task.slug)}"${
            step && step.task === task.slug ? " selected" : ""
          }>${esc(task.title)}</option>`
      ),
    ].join("");
    box.hidden = false;
    box.innerHTML = `
      <div class="dg-editor-grid">
        <label class="sy-field"><span>Use case</span>
          <input id="uc-title" class="sy-input" value="${esc(useCase.title)}"></label>
        <label class="sy-field"><span>Actor</span>
          <input id="uc-actor" class="sy-input" value="${esc(
            useCase.actor || ""
          )}" placeholder="Operator"></label>
        <label class="sy-field dg-wide"><span>Summary</span>
          <input id="uc-summary" class="sy-input" value="${esc(
            useCase.summary || ""
          )}" placeholder="one line of intent"></label>
        ${
          step
            ? `<label class="sy-field"><span>Step ${picked + 1} label</span>
          <input id="step-label" class="sy-input" value="${esc(step.label)}"></label>
        <label class="sy-field"><span>Kind</span>
          <select id="step-kind" class="sy-select">
            <option value="activity"${step.kind === "choice" ? "" : " selected"}>Activity</option>
            <option value="choice"${step.kind === "choice" ? " selected" : ""}>Choice — decision diamond</option>
          </select></label>
        <label class="sy-field dg-wide"><span>Implemented by</span>
          <select id="step-task" class="sy-select">${options}</select></label>
        ${step.kind === "choice" ? branchRowsHtml(useCase, picked, step) : ""}`
            : '<p class="muted dg-wide">Click a step in the diagram to edit it, or add one below.</p>'
        }
      </div>
      <div class="dg-editor-actions">
        ${
          step
            ? `<button class="sy-btn ghost" type="button" data-use-action="left" ${
                picked === 0 ? "disabled" : ""
              }>Move earlier</button>
        <button class="sy-btn ghost" type="button" data-use-action="right" ${
          picked === useCase.steps.length - 1 ? "disabled" : ""
        }>Move later</button>
        <button class="sy-btn ghost" type="button" data-use-action="remove">Remove step</button>`
            : ""
        }
        <button class="sy-btn ghost" type="button" data-use-action="add">Add step</button>
        <button class="sy-btn primary" type="button" data-use-action="save">Save workflow</button>
      </div>`;
  }

  function addUseCase() {
    const doc = useCaseDoc();
    if (!doc) return;
    readUseEditor();
    let number = doc.use_cases.length + 1;
    const ids = new Set(doc.use_cases.map((row) => row.id));
    while (ids.has(`uc${number}`)) number += 1;
    const useCase = {
      id: `uc${number}`,
      title: `Use case ${number}`,
      actor: null,
      summary: null,
      steps: [{ label: "Start", task: null }],
    };
    doc.use_cases.push(useCase);
    state.specTab = "usecases";
    state.activeUseCase = useCase.id;
    state.expanded = [...state.expanded, useCase.id];
    state.useStep = null;
    state.useDirty = true;
    renderShapeSpec();
  }

  async function saveUseCases() {
    readUseEditor();
    const doc = state.useCases?.use_cases;
    const project = state.shapeProject;
    if (!doc || !project) return;
    for (const useCase of doc.use_cases) {
      if (useCase.steps.some((step) => step.kind === "choice")) {
        ensureStepIds(useCase);
      }
    }
    try {
      const result = await api(
        `/api/shapes/${encodeURIComponent(project)}/use-cases`,
        { method: "PUT", body: doc }
      );
      state.useCases = {
        shape: result.shape,
        state: result.state,
        use_cases: result.use_cases,
        editable: result.editable,
      };
      state.useDirty = false;
      if (state.shapeData?.shape) {
        state.shapeData.shape.state = result.state;
        state.shapeData.shape.check = result.check;
      }
      flash("Workflow saved.");
      renderShapePage();
    } catch (error) {
      flash(error.message, true);
    }
  }

  async function extrapolateModels() {
    const project = state.shapeProject;
    if (!project) return;
    const base = `/api/shapes/${encodeURIComponent(project)}/jobs`;
    try {
      await api(base, { method: "POST", body: { kind: "workflows" } });
      await api(base, { method: "POST", body: { kind: "c4" } });
      flash("Extrapolating use cases and the C4 model — two turns.");
      await loadShapePage();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function c4Signature(levelKey) {
    return JSON.stringify(["c4", levelKey, state.c4Element, c4Level(levelKey)]);
  }

  function c4RelationshipRows(level, box) {
    return level.relationships
      .map((relationship, index) => {
        const options = (selected) =>
          level.elements
            .map(
              (element) =>
                `<option value="${esc(element.id)}"${
                  element.id === selected ? " selected" : ""
                }>${esc(element.name)}</option>`
            )
            .join("");
        return `<div class="c4-rel-row">
          <select class="sy-select" data-c4-from="${index}" aria-label="From">${options(
            relationship.from
          )}</select>
          <input class="sy-input" data-c4-label="${index}" value="${esc(
            relationship.label
          )}" aria-label="Label">
          <select class="sy-select" data-c4-to="${index}" aria-label="To">${options(
            relationship.to
          )}</select>
          <input class="sy-input" data-c4-reltech="${index}" value="${esc(
            relationship.technology || ""
          )}" placeholder="tech" aria-label="Technology">
          <button class="sy-btn ghost" type="button" data-c4-action="remove-rel" data-rel="${index}" title="remove">×</button>
        </div>`;
      })
      .join("");
  }

  function renderC4Editor(levelKey) {
    const box = $("shape-use-editor");
    const level = c4Level(levelKey);
    const selected = level.elements.find(
      (element) => element.id === state.c4Element
    );
    const signature = c4Signature(levelKey);
    if (!box.hidden && box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    const typeOptions = C4_TYPES.map(
      (type) =>
        `<option value="${type}"${
          selected && selected.type === type ? " selected" : ""
        }>${type}</option>`
    ).join("");
    box.hidden = false;
    box.innerHTML = `
      <div class="dg-editor-grid">
        ${
          selected
            ? `<label class="sy-field"><span>Name</span>
          <input id="c4-name" class="sy-input" value="${esc(selected.name)}"></label>
        <label class="sy-field"><span>Type</span>
          <select id="c4-type" class="sy-select">${typeOptions}</select></label>
        <label class="sy-field"><span>Technology</span>
          <input id="c4-tech" class="sy-input" value="${esc(
            selected.technology || ""
          )}" placeholder="python, postgres…"></label>
        <label class="sy-field"><span>Id</span>
          <input class="sy-input" value="${esc(selected.id)}" disabled></label>
        <label class="sy-field dg-wide"><span>Description</span>
          <input id="c4-desc" class="sy-input" value="${esc(
            selected.description || ""
          )}" placeholder="what it is, in one line"></label>`
            : '<p class="muted dg-wide">Click an element to edit it, or add one below.</p>'
        }
        <div class="dg-wide uc-branches">
          <div class="uc-branch-head"><span>Relationships</span><span>${
            level.relationships.length
          }</span></div>
          ${c4RelationshipRows(level, box)}
          <button class="sy-btn ghost" type="button" data-c4-action="add-rel" ${
            level.elements.length < 1 ? "disabled" : ""
          }>Add relationship</button>
        </div>
      </div>
      <div class="dg-editor-actions">
        ${
          selected
            ? '<button class="sy-btn ghost" type="button" data-c4-action="remove">Remove element</button>'
            : ""
        }
        <button class="sy-btn ghost" type="button" data-c4-action="add">Add element</button>
        <button class="sy-btn primary" type="button" data-c4-action="save">Save C4 model</button>
      </div>`;
  }

  function readC4Editor(levelKey) {
    const level = c4Level(levelKey);
    const box = $("shape-use-editor");
    const selected = level.elements.find(
      (element) => element.id === state.c4Element
    );
    if (selected) {
      if ($("c4-name")) selected.name = $("c4-name").value.trim() || selected.name;
      if ($("c4-type")) selected.type = $("c4-type").value;
      if ($("c4-tech")) selected.technology = $("c4-tech").value.trim() || null;
      if ($("c4-desc")) selected.description = $("c4-desc").value.trim() || null;
    }
    const labels = [...box.querySelectorAll("[data-c4-label]")];
    if (labels.length) {
      const froms = [...box.querySelectorAll("[data-c4-from]")];
      const tos = [...box.querySelectorAll("[data-c4-to]")];
      const techs = [...box.querySelectorAll("[data-c4-reltech]")];
      level.relationships = labels
        .map((input, index) => ({
          from: froms[index]?.value || "",
          to: tos[index]?.value || "",
          label: input.value.trim() || "uses",
          technology: techs[index]?.value.trim() || null,
        }))
        .filter((relationship) => relationship.from && relationship.to);
    }
  }

  async function saveC4(levelKey) {
    readC4Editor(levelKey);
    const doc = c4Doc();
    const project = state.shapeProject;
    if (!doc || !project) return;
    try {
      const result = await api(
        `/api/shapes/${encodeURIComponent(project)}/c4`,
        { method: "PUT", body: doc }
      );
      state.c4 = {
        shape: result.shape,
        state: result.state,
        c4: result.c4,
        editable: result.editable,
      };
      state.c4Dirty = false;
      if (state.shapeData?.shape) {
        state.shapeData.shape.state = result.state;
        state.shapeData.shape.check = result.check;
      }
      flash("C4 model saved.");
      renderShapePage();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function c4Action(action, extra) {
    if (action === "save") {
      saveC4(state.specTab);
      return;
    }
    const levelKey = state.specTab;
    const level = c4Level(levelKey);
    readC4Editor(levelKey);
    if (action === "add") {
      let number = level.elements.length + 1;
      const ids = new Set(level.elements.map((element) => element.id));
      while (ids.has(`element-${number}`)) number += 1;
      level.elements.push({
        id: `element-${number}`,
        type: C4_DEFAULT_TYPE[levelKey] || "system",
        name: `Element ${number}`,
        technology: null,
        description: null,
      });
      state.c4Element = `element-${number}`;
      state.c4Dirty = true;
      renderShapeSpec();
      $("c4-name")?.focus();
      return;
    }
    if (action === "remove") {
      const selected = level.elements.find(
        (element) => element.id === state.c4Element
      );
      if (!selected) return;
      level.elements = level.elements.filter(
        (element) => element.id !== selected.id
      );
      level.relationships = level.relationships.filter(
        (relationship) =>
          relationship.from !== selected.id &&
          relationship.to !== selected.id
      );
      state.c4Element = null;
      state.c4Dirty = true;
      renderShapeSpec();
      return;
    }
    if (action === "add-rel") {
      if (!level.elements.length) {
        flash("Add an element first.", true);
        return;
      }
      const from = level.elements[0].id;
      const to = level.elements[1]?.id || from;
      level.relationships.push({
        from,
        to,
        label: "uses",
        technology: null,
      });
      state.c4Dirty = true;
      renderShapeSpec();
      return;
    }
    if (action === "remove-rel") {
      const index = Number(extra);
      level.relationships.splice(index, 1);
      state.c4Dirty = true;
      renderShapeSpec();
      return;
    }
    if (action === "retype") {
      state.c4Dirty = true;
      renderShapeSpec();
    }
  }

  async function useAction(action, extra) {
    if (action === "save") {
      await saveUseCases();
      return;
    }
    const useCase = specUseCases().find(
      (row) => row.id === state.activeUseCase
    );
    if (!useCase) return;
    readUseEditor();
    const picked =
      state.useStep && state.useStep.uc === useCase.id ? state.useStep.index : null;

    if (action === "kind") {
      const step = picked != null ? useCase.steps[picked] : null;
      if (!step) return;
      if (step.kind === "choice") ensureStepIds(useCase);
      else step.branches = [];
      state.useDirty = true;
      renderShapeSpec();
      return;
    }
    if (action === "add") {
      useCase.steps.push({ label: "New step", task: null });
      state.useStep = { uc: useCase.id, index: useCase.steps.length - 1 };
      state.useDirty = true;
      renderShapeSpec();
      $("step-label")?.focus();
      return;
    }
    if (action === "add-branch") {
      const step = picked != null ? useCase.steps[picked] : null;
      if (!step) return;
      ensureStepIds(useCase);
      step.branches = step.branches || [];
      if (step.branches.length >= 4) {
        flash("Four branches is the limit.", true);
        return;
      }
      const later = useCase.steps.slice(picked + 1);
      const target = later.find((row) => row.id)?.id;
      if (!target) {
        flash("Add a later step first; a branch needs somewhere to go.", true);
        return;
      }
      const guards = ["yes", "no", "else", "timeout"];
      step.branches.push({
        guard: guards[step.branches.length] || "branch",
        to: target,
      });
      state.useDirty = true;
      renderShapeSpec();
      return;
    }
    if (action === "remove-branch") {
      const index = Number(extra);
      const step = picked != null ? useCase.steps[picked] : null;
      if (!step) return;
      step.branches = (step.branches || []).filter((_, row) => row !== index);
      state.useDirty = true;
      renderShapeSpec();
      return;
    }
    if (picked == null) return;
    if (action === "left" && picked > 0) {
      [useCase.steps[picked - 1], useCase.steps[picked]] = [
        useCase.steps[picked],
        useCase.steps[picked - 1],
      ];
      if (!branchesValid(useCase)) {
        [useCase.steps[picked - 1], useCase.steps[picked]] = [
          useCase.steps[picked],
          useCase.steps[picked - 1],
        ];
        flash("That move would point a branch backwards.", true);
        return;
      }
      state.useStep = { uc: useCase.id, index: picked - 1 };
    } else if (action === "right" && picked < useCase.steps.length - 1) {
      [useCase.steps[picked + 1], useCase.steps[picked]] = [
        useCase.steps[picked],
        useCase.steps[picked + 1],
      ];
      if (!branchesValid(useCase)) {
        [useCase.steps[picked + 1], useCase.steps[picked]] = [
          useCase.steps[picked],
          useCase.steps[picked + 1],
        ];
        flash("That move would point a branch backwards.", true);
        return;
      }
      state.useStep = { uc: useCase.id, index: picked + 1 };
    } else if (action === "remove") {
      if (useCase.steps.length === 1) {
        flash("A use case needs at least one step.", true);
        return;
      }
      const removed = useCase.steps[picked];
      useCase.steps.splice(picked, 1);
      if (removed?.id) {
        for (const step of useCase.steps) {
          step.branches = (step.branches || []).filter(
            (branch) => branch.to !== removed.id
          );
        }
      }
      state.useStep = null;
    } else {
      return;
    }
    state.useDirty = true;
    renderShapeSpec();
  }

  function renderShapePage() {
    const project = state.shapeProject;
    if (!project) return;
    const data = state.shapeData;
    const shape = data?.shape || null;
    const questions = data?.questions?.questions || [];
    $("shape-title").textContent = projectName(project);
    const phase = projectById(project)?.spec_phase;
    $("shape-meta").innerHTML = shape
      ? badge(shape.state)
      : phase === "approved"
        ? badge("approved")
        : badge("not started");
    $("shape-error").hidden = !state.shapeError;
    $("shape-error").textContent = state.shapeError;
    const revision = document.querySelector(".shape-revision");
    if (revision) revision.classList.toggle("single", !shape);
    // The page polls while a turn runs; never wipe what the operator is typing.
    const carry = [];
    for (const field of document.querySelectorAll("#shape-body [data-shape-q]")) {
      if (!field.value && document.activeElement !== field) continue;
      carry.push({
        id: field.dataset.shapeQ,
        value: field.value,
        start: field.selectionStart,
        end: field.selectionEnd,
        focus: document.activeElement === field,
      });
    }
    $("shape-body").innerHTML = shape
      ? shapeBodyHtml(shape, questions)
      : shapeStartHtml(project);
    for (const item of carry) {
      const field = document.querySelector(
        `#shape-body [data-shape-q="${item.id}"]`
      );
      if (!field) continue;
      field.value = item.value;
      if (item.focus) {
        field.focus();
        try {
          field.setSelectionRange(item.start, item.end);
        } catch (error) {
          // selection is best-effort
        }
      }
    }
    for (const field of document.querySelectorAll("#shape-body [data-shape-q]")) {
      growField(field);
    }
    updateAnswerAction();
    $("shape-diff").hidden = !state.shapeDiff;
    $("shape-diff").textContent = state.shapeDiff;
    renderShapeModel();
    renderShapeSession();
    renderShapeTree();
    renderShapeSpec();
    renderShapeDivider(shape, questions);
  }

  function renderShapeDivider(shape, questions) {
    const approve = $("shape-approve");
    const cancel = $("shape-cancel");
    const check = $("shape-check");
    if (!approve || !cancel || !check) return;
    const visible = Boolean(shape);
    approve.hidden = !visible;
    cancel.hidden = !visible;
    check.hidden = !visible;
    if (!visible) return;
    const open = questions.filter(
      (question) => !(question.answer || "").trim()
    ).length;
    const busy = ["drafting", "revising"].includes(shape.state);
    const canApprove = ["ready", "materialized"].includes(shape.state);
    approve.disabled = busy || !canApprove;
    approve.title = busy
      ? "wait for the turn to finish"
      : open
        ? `${open} question${open === 1 ? "" : "s"} left`
        : canApprove
          ? "materialize and merge this spec"
          : "fix the check first";
    cancel.disabled = busy;
    cancel.title = busy ? "wait for the turn to finish" : "discard this shape";
    check.disabled = busy;
    check.title = busy ? "wait for the turn to finish" : "run the pristine check now";
  }

  async function submitAnswer(field) {
    const questionId = field.dataset.shapeQ;
    const value = field.value.trim();
    if (!questionId || !value) return;
    const fields = [...document.querySelectorAll("#shape-body [data-shape-q]")];
    await queueAnswers({ [questionId]: value }, fields.indexOf(field));
  }

  async function shapeAction(action) {
    const project = state.shapeProject;
    if (!project) return;
    const base = `/api/shapes/${encodeURIComponent(project)}`;
    try {
      if (action === "answer-revise") {
        await queueAnswers(collectAnswers());
        return;
      }
      if (action === "start") {
        const seed = state.shapeSeed || {};
        const shape = await api("/api/shapes", {
          method: "POST",
          body: {
            project,
            objective: seed.objective || null,
            task: seed.task || null,
            rewrite: false,
          },
        });
        state.shapeSeed = null;
        state.shapeData = { shape, questions: null };
        flash("Shape started.");
      } else if (action === "revise") {
        await api(`${base}/jobs`, {
          method: "POST",
          body: { kind: "revise", message: "" },
        });
        flash("Revision queued.");
      } else if (action === "check") {
        await api(`${base}/check`, { method: "POST" });
      } else if (action === "materialize") {
        await api(`${base}/materialize`, { method: "POST" });
        flash("Materialized.");
      } else if (action === "approve") {
        if (!confirm("Approve this shape? It materializes the decomposition and merges the spec.")) return;
        if (state.shapeData?.shape?.state === "ready") {
          await api(`${base}/materialize`, { method: "POST" });
        }
        await api(`${base}/approve`, { method: "POST" });
        flash("Spec approved.");
      } else if (action === "cancel") {
        if (!confirm("Cancel the active shape?")) return;
        await api(`${base}/cancel`, { method: "POST" });
        flash("Shape cancelled.");
      } else if (action === "diff") {
        const data = await api(`${base}/diff`);
        state.shapeDiff = state.shapeDiff ? "" : data.patch || "(no diff)";
        renderShapePage();
        return;
      }
      await loadShapePage();
      await loadTasks();
    } catch (error) {
      state.shapeError = workErrorText(error);
      renderShapePage();
    }
  }

  function fillShapeDefaults() {
    const questions = state.workQuestions?.questions || [];
    for (const question of questions) {
      const input = document.querySelector(`[data-work-question="${CSS.escape(question.id)}"]`);
      if (input && !input.value.trim()) input.value = question.default || "";
    }
  }

  async function loadShapeDiff() {
    const task = currentWorkTask();
    if (!task) return;
    try {
      const data = await api(`/api/shapes/${encodeURIComponent(task.project)}/diff`);
      state.workDiff = data.patch || "(no diff)";
      renderWork();
    } catch (error) {
      showWorkError(workErrorText(error));
    }
  }

  async function buildFromWork() {
    const task = currentWorkTask();
    if (!task) return;
    const model = state.workModel;
    if (!model) { showWorkError("Select a model."); return; }
    const agendaDraft = state.workAgendaItem && state.agenda?.status === "draft";
    if (agendaDraft && !confirm("Lock today's agenda and start this build?")) return;
    state.workError = "";
    state.workBusy = true;
    renderWork();
    try {
      if (agendaDraft) {
        const item = state.agenda?.items?.find((row) => row.id === state.workAgendaItem?.id);
        if (item && item.objective !== task.objective) {
          item.objective = task.objective;
          markDirty();
        }
        if (state.agendaDirty) {
          await saveAgenda();
          if (state.agendaDirty) throw new Error("Agenda changes were not saved.");
        }
        const result = await api(`/api/agendas/${state.agendaDate}/lock`, {
          method: "POST",
          body: { if_rev: state.agenda.rev || 0 },
        });
        state.agenda = result.agenda;
        state.agendaDirty = false;
        renderToday();
      }
      localStorage.setItem("cc.model", model);
      state.workModel = model;
      const run = await api("/api/runs", {
        method: "POST",
        body: { task: task.task, model, reviewer: state.workReviewer || null },
      });
      stopWorkPoll();
      flash(`Started ${run.id}.`);
      await loadRuns();
      go("builder");
      await selectRun(run.id);
    } catch (error) {
      state.workError = workErrorText(error);
      await refreshWork({ context: true }).catch(() => {});
    } finally {
      state.workBusy = false;
      if (state.view === "work") renderWork();
    }
  }

  function openWorkRun(runId) {
    stopWorkPoll();
    go("builder");
    selectRun(runId);
  }

  async function workAction(action, runId = null) {
    if (action === "open-shape") {
      const task = currentWorkTask();
      if (task) {
        openShapePage(task.project, { objective: task.objective, task: task.task });
      }
      return;
    }
    if (state.workBusy && action !== "open-run") return;
    if (action === "start-shape") await startShape();
    else if (action === "answer-revise") await answerAndRevise();
    else if (action === "revise-shape") await reviseShape();
    else if (action === "materialize-shape") await materializeShape();
    else if (action === "approve-shape") await approveShape();
    else if (action === "cancel-shape") await cancelShape();
    else if (action === "shape-diff") await loadShapeDiff();
    else if (action === "fill-defaults") fillShapeDefaults();
    else if (action === "build") await buildFromWork();
    else if (action === "open-run" && runId) openWorkRun(runId);
  }

  // -------------------------------------------------------------- builder
  function builderRun(item) {
    if (item.run) return state.runs.find((run) => run.id === item.run) || null;
    return workLatestRun(item.task);
  }

  function elapsedText(startedAt) {
    const started = new Date(startedAt).getTime();
    if (!Number.isFinite(started)) return "—";
    const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function builderEntries() {
    const items = state.agenda?.items || [];
    return items.map((item) => {
      const task = taskById(item.task);
      const runs = state.runs
        .filter((row) => row.task === item.task)
        .sort((a, b) => String(b.created).localeCompare(String(a.created)));
      const active = runs.find((row) => ACTIVE_STATES.has(row.state)) || null;
      const latest = runs[0] || null;
      const run = active || latest;
      let status = "pending";
      if (active) status = "running";
      else if (latest?.state === "merged") status = "done";
      else if (latest && TERMINAL_STATES.has(latest.state)) status = "failed";
      return { item, task, run, status };
    });
  }

  function builderMeta(entry) {
    if (entry.status === "running") {
      return [
        "running",
        entry.run?.started_at ? elapsedText(entry.run.started_at) : "",
        entry.run?.attempt ? `attempt ${entry.run.attempt}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
    }
    if (entry.status === "done") {
      return ["merged", entry.run?.merge_sha ? entry.run.merge_sha.slice(0, 8) : ""]
        .filter(Boolean)
        .join(" · ");
    }
    if (entry.status === "failed") return entry.run?.state || "failed";
    return entry.task?.ready
      ? "ready"
      : entry.task?.reasons?.[0]?.code || "not started";
  }

  function renderBuilder() {
    const entries = builderEntries();
    const live = entries.filter((entry) => entry.status === "running").length;
    const done = entries.filter((entry) => entry.status === "done").length;
    $("builder-sub").textContent = entries.length
      ? `${entries.length} task${entries.length === 1 ? "" : "s"} · ${live} running · ${done} done`
      : "No agenda for today";
    $("builder-empty").hidden = entries.length > 0;
    $("builder-process-card").hidden = entries.length === 0;
    if (!entries.length) {
      $("builder-session").hidden = true;
      $("builder-decomposition").hidden = true;
      $("builder-spec").hidden = true;
      $("builder-build").hidden = true;
      return;
    }
    if (!state.selectedRun) {
      const active = entries.find((entry) => entry.status === "running");
      if (active?.run) {
        // Load the live session for whatever is building right now.
        selectBuilderRun(active.run.id);
        return;
      }
    }
    const signature = JSON.stringify(
      entries.map((entry) => [
        entry.item.task,
        entry.status,
        entry.run?.id || "",
        entry.run?.started_at || "",
        entry.run?.merge_sha || "",
      ])
    );
    if ($("builder-process").dataset.signature !== signature) {
      $("builder-process").dataset.signature = signature;
      $("builder-process-meta").textContent = `${entries.length} tasks · ${live} running · ${done} done`;
      if (window.renderProcessDiagram) {
        window.renderProcessDiagram(
          $("builder-process"),
          entries.map((entry) => ({
            task: entry.item.task,
            run: entry.run?.id || "",
            status: entry.status,
            title: entry.task?.title || entry.item.task,
            meta: builderMeta(entry),
          })),
          {
            selected: state.selectedRun?.id || "",
            onSelect: (row) => {
              if (row.run) selectBuilderRun(row.run);
              else showWorkById(row.task);
            },
          }
        );
      }
    }
    renderBuilderSession();
    const project = builderProject();
    renderBuilderDecomposition(project);
    renderBuilderSpec(project);
    renderBuildControl(project);
    if (
      project &&
      (state.builderBuildProject !== project ||
        state.builderBuild?.active)
    ) {
      refreshProjectBuild(project);
    }
  }

  function renderBuildControl(project) {
    const button = $("builder-build");
    const note = $("builder-build-note");
    if (!button || !note) return;
    if (!project) {
      button.hidden = true;
      note.textContent = "";
      return;
    }
    button.hidden = false;
    const status = state.builderBuild?.project === project ? state.builderBuild : null;
    if (status?.active) {
      button.textContent = "Stop";
      button.className = "sy-btn ghost";
      note.textContent = `building · ${status.merged} merged${
        status.current_run ? ` · ${status.current_run}` : ""
      }`;
    } else {
      button.textContent = "Build project";
      button.className = "sy-btn primary";
      note.textContent = status?.note || "";
    }
  }

  async function refreshProjectBuild(project) {
    if (!project) return;
    try {
      const status = await api(
        `/api/projects/${encodeURIComponent(project)}/build`
      );
      state.builderBuild = status;
      state.builderBuildProject = project;
      renderBuildControl(project);
      if (state.builderBuildTimer) {
        clearTimeout(state.builderBuildTimer);
        state.builderBuildTimer = null;
      }
      if (status.active) {
        state.builderBuildTimer = setTimeout(
          () => refreshProjectBuild(project),
          2000
        );
      }
    } catch (error) {
      state.builderBuild = null;
      renderBuildControl(project);
    }
  }

  async function toggleProjectBuild() {
    const project = builderProject();
    if (!project) return;
    const active = state.builderBuild?.project === project && state.builderBuild?.active;
    try {
      if (active) {
        await api(`/api/projects/${encodeURIComponent(project)}/build/stop`, {
          method: "POST",
        });
        flash("Stopping after the current step.");
      } else {
        await api(`/api/projects/${encodeURIComponent(project)}/build`, {
          method: "POST",
          body: {},
        });
        flash(`Building ${projectName(project)} task by task.`);
      }
      await refreshProjectBuild(project);
      await loadRuns();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function builderProject() {
    return (
      state.selectedRun?.project ||
      builderEntries()[0]?.item.project ||
      null
    );
  }

  function builderTaskStatus(taskId) {
    const runs = state.runs.filter((row) => row.task === taskId);
    if (runs.some((row) => ACTIVE_STATES.has(row.state))) return "running";
    if (runs.some((row) => row.state === "merged")) return "done";
    if (runs.some((row) => TERMINAL_STATES.has(row.state))) return "failed";
    return "pending";
  }

  function builderRunFor(taskId) {
    const runs = state.runs.filter((row) => row.task === taskId);
    return (
      runs.find((row) => ACTIVE_STATES.has(row.state)) ||
      runs.find((row) => row.state === "merged") ||
      runs[0] ||
      null
    );
  }

  function renderBuilderDecomposition(project) {
    const card = $("builder-decomposition");
    if (!project) {
      card.hidden = true;
      return;
    }
    const tasks = state.tasks.filter((task) => task.project === project);
    if (!tasks.length) {
      card.hidden = true;
      return;
    }
    const groups = new Map();
    for (const task of tasks) {
      const key = task.objective || "—";
      if (!groups.has(key)) {
        groups.set(key, {
          title: task.objective_title || key,
          tasks: [],
        });
      }
      groups.get(key).tasks.push(task);
    }
    const done = tasks.filter((task) => builderTaskStatus(task.task) === "done").length;
    const running = tasks.filter((task) => builderTaskStatus(task.task) === "running").length;
    card.hidden = false;
    $("builder-decomp-meta").textContent = `${tasks.length} tasks · ${done} done · ${running} building`;
    $("builder-decomp").innerHTML = [...groups.values()]
      .map(
        (group) => `<div class="decomp-band">
          <div class="decomp-band-head"><span>${esc(group.title)}</span>
            <span>${group.tasks.length} task${group.tasks.length === 1 ? "" : "s"}</span></div>
          <div class="decomp-cards">${group.tasks
            .map((task) => builderDecompCard(task))
            .join("")}</div>
        </div>`
      )
      .join("");
  }

  function builderDecompCard(task) {
    const status = builderTaskStatus(task.task);
    const meta =
      status === "running"
        ? "building"
        : status === "done"
          ? "merged"
          : status === "failed"
            ? "failed"
            : task.ready
              ? "ready"
              : task.reasons?.[0]?.code || "not started";
    return `<button type="button" class="decomp-card status-${status}" data-decomp-task="${esc(
      task.task
    )}">
      <span class="dg-title">${esc(task.title)}</span>
      <span class="decomp-meta">${esc(meta)}</span>
    </button>`;
  }

  async function ensureBuilderSpec(project) {
    if (!project || state.builderSpecLoading) return;
    if (state.builderSpec?.project === project) return;
    state.builderSpecLoading = project;
    try {
      const data = await api(
        `/api/projects/${encodeURIComponent(project)}/spec`
      );
      state.builderSpec = { ...data, project };
    } catch (error) {
      state.builderSpec = {
        project,
        files: [],
        use_cases: null,
        c4: null,
        ref: "",
        errors: [error.message],
      };
    } finally {
      state.builderSpecLoading = null;
    }
    if (state.view === "builder") renderBuilderSpec(project);
  }

  function renderBuilderSpec(project) {
    const card = $("builder-spec");
    if (!project) {
      card.hidden = true;
      return;
    }
    const data = state.builderSpec?.project === project ? state.builderSpec : null;
    if (!data) {
      card.hidden = false;
      $("builder-spec-meta").textContent = "loading";
      $("builder-spec-tabs").innerHTML = "";
      $("builder-spec-body").innerHTML =
        '<p class="muted">Reading the spec from the repository…</p>';
      ensureBuilderSpec(project);
      return;
    }
    const files = data.files || [];
    const cases = data.use_cases?.use_cases || [];
    const c4 = data.c4;
    const tabs = [];
    if (files.length) tabs.push(["md", "Markdown"]);
    if (cases.length) tabs.push(["usecases", "Use cases"]);
    if (c4) {
      for (const [id, label] of C4_LEVELS) tabs.push([id, label]);
    }
    if (!tabs.length) {
      card.hidden = true;
      return;
    }
    if (!tabs.some(([id]) => id === state.builderSpecTab)) {
      state.builderSpecTab = tabs[0][0];
    }
    card.hidden = false;
    $("builder-spec-meta").textContent = [
      data.ref,
      files.length ? `${files.length} doc${files.length === 1 ? "" : "s"}` : "",
      cases.length
        ? `${cases.length} use case${cases.length === 1 ? "" : "s"}`
        : "",
    ]
      .filter(Boolean)
      .join(" · ");
    $("builder-spec-tabs").innerHTML = tabs
      .map(
        ([id, label]) =>
          `<button type="button" class="spec-tab${
            state.builderSpecTab === id ? " active" : ""
          }" data-builder-spec-tab="${id}">${esc(label)}</button>`
      )
      .join("");
    const tab = state.builderSpecTab;
    if (tab === "md") {
      $("builder-spec-body").innerHTML = `<div class="spec-docs">${files
        .map(
          (file) =>
            `<article class="spec-doc"><div class="spec-doc-name">${esc(
              file.path
            )}</div>${
              window.renderMarkdown
                ? window.renderMarkdown(file.text)
                : `<pre>${esc(file.text)}</pre>`
            }</article>`
        )
        .join("")}</div>`;
      return;
    }
    $("builder-spec-body").innerHTML =
      '<div class="dg-wrap"><div id="builder-spec-diagram"></div></div>';
    const box = $("builder-spec-diagram");
    if (tab === "usecases") {
      const collapsed = new Set(
        cases
          .map((useCase) => useCase.id)
          .filter((id) => !state.builderSpecExpanded.includes(id))
      );
      if (window.renderUseCaseDiagram) {
        window.renderUseCaseDiagram(box, { use_cases: cases }, {
          collapsed,
          onToggle: (id) => {
            const at = state.builderSpecExpanded.indexOf(id);
            if (at >= 0) state.builderSpecExpanded.splice(at, 1);
            else state.builderSpecExpanded.push(id);
            renderBuilderSpec(project);
          },
        });
      }
      return;
    }
    if (window.renderC4Diagram) {
      window.renderC4Diagram(box, c4.levels[tab], {});
    }
  }

  function resumeBuilderSession() {
    if (!state.selectedRun || !ACTIVE_STATES.has(state.selectedRun.state)) {
      return;
    }
    if (!state.builderTimer) startBuilderPoll();
    if (state.builderTab === "stream" && !state.streamEntries.length) {
      pollBuilderStream();
    }
  }

  function renderBuilderSession() {
    const card = $("builder-session");
    const run = state.selectedRun;
    card.hidden = !run;
    if (!run) return;
    $("builder-vitals").innerHTML = `
      <div class="vitals-left">
        <span class="vital">${esc(run.state)}</span>
        <span class="vital" id="builder-elapsed">${
          run.started_at ? elapsedText(run.started_at) : "—"
        }</span>
        <span class="vital">attempt ${esc(run.attempt)}</span>
      </div>
      <div class="vitals-right">
        <span class="vital">coder ${esc(
          run.model_resolved || run.model_requested || "—"
        )}</span>
        <span class="vital">reviewer ${esc(
          run.reviewer_resolved || run.reviewer_model || "—"
        )}</span>
      </div>`;
    const tabs = [
      ["stream", "Stream"],
      ["diff", "Diff"],
      ["gate", "Gate"],
      ["policy", "Policy"],
      ["review", "Review"],
    ];
    $("builder-tabs").innerHTML = tabs
      .map(
        ([id, label]) =>
          `<button class="artifact-tab${
            state.builderTab === id ? " active" : ""
          }" type="button" data-builder-tab="${id}">${label}</button>`
      )
      .join("");
    const terminal = TERMINAL_STATES.has(run.state);
    $("builder-cancel").hidden = terminal;
    $("builder-cancel").disabled = terminal;
    $("builder-follow").textContent = state.builderFollow
      ? "Follow on"
      : "Follow off";
    const streaming = state.builderTab === "stream";
    $("builder-stream").hidden = !streaming;
    $("builder-artifact").hidden = streaming;
    if (streaming) renderBuilderStream();
  }

  function renderBuilderStream() {
    const box = $("builder-stream");
    const entries = state.streamEntries || [];
    box.innerHTML = entries.length
      ? entries
          .map((entry) =>
            entry.kind === "text"
              ? `<p class="stream-text">${esc(entry.text)}</p>`
              : `<div class="stream-tool"><b>${esc(entry.tool)}</b><span>${esc(
                  entry.text
                )}</span></div>`
          )
          .join("")
      : '<div class="stream-empty">Waiting for the agent…</div>';
    if (state.builderFollow) box.scrollTop = box.scrollHeight;
  }

  function toolSummary(tool, input) {
    const first = (value) =>
      String(value || "").split("\n")[0].trim().slice(0, 120);
    if (tool === "shell") return first(input.command);
    if (tool === "read" || tool === "write" || tool === "edit") {
      return first(input.filePath || input.path);
    }
    if (tool === "grep" || tool === "glob") return first(input.pattern);
    if (tool === "webfetch") return first(input.url);
    if (tool === "task") return first(input.description || input.prompt);
    const value = Object.values(input)[0];
    return value ? first(value) : "";
  }

  function ingestStream(text) {
    state.streamRaw += text;
    const lines = state.streamRaw.split("\n");
    state.streamRaw = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch (error) {
        continue;
      }
      const part = event.part || {};
      if (event.type === "text") {
        const value = (part.text || "").trim();
        if (value) state.streamEntries.push({ kind: "text", text: value });
      } else if (event.type === "tool_use") {
        state.streamEntries.push({
          kind: "tool",
          tool: part.tool || "tool",
          text: toolSummary(part.tool || "", part.state?.input || {}),
        });
      }
    }
  }

  async function pollBuilderStream() {
    const run = state.selectedRun;
    if (!run) return;
    try {
      const chunk = await api(
        `/api/runs/${run.id}/logs?stream=agent&offset=${state.logOffset}`
      );
      if (state.logAttempt !== chunk.attempt) {
        state.logOffset = 0;
        state.logAttempt = chunk.attempt;
        state.streamRaw = "";
        state.streamEntries = [];
      }
      state.logOffset = chunk.offset;
      if (chunk.text) {
        ingestStream(chunk.text);
        if (state.builderTab === "stream") renderBuilderStream();
      }
    } catch (error) {
      /* a run that has not written a log yet is not an error */
    }
  }

  function startBuilderPoll() {
    stopBuilderPoll();
    if (!state.selectedRun || !ACTIVE_STATES.has(state.selectedRun.state)) {
      return;
    }
    state.builderTimer = setInterval(async () => {
      if (!state.selectedRun) return stopBuilderPoll();
      await pollBuilderStream();
      await refreshSelectedRun();
    }, 1500);
    state.builderClock = setInterval(() => {
      const node = $("builder-elapsed");
      if (node && state.selectedRun?.started_at) {
        node.textContent = elapsedText(state.selectedRun.started_at);
      }
    }, 1000);
  }

  function stopBuilderPoll() {
    if (state.builderTimer) {
      clearInterval(state.builderTimer);
      state.builderTimer = null;
    }
    if (state.builderClock) {
      clearInterval(state.builderClock);
      state.builderClock = null;
    }
  }

  async function loadBuilderTab() {
    if (!state.selectedRun) return;
    if (state.builderTab === "stream") {
      renderBuilderStream();
      await pollBuilderStream();
      return;
    }
    const run = state.selectedRun;
    const kind = state.builderTab;
    try {
      const value = await api(`/api/runs/${run.id}/artifacts/${kind}`);
      $("builder-artifact").textContent =
        typeof value === "string" ? value : JSON.stringify(value, null, 2);
    } catch (error) {
      $("builder-artifact").textContent = `(${kind} not available)`;
    }
  }

  async function selectBuilderRun(id) {
    try {
      const run = await api(`/api/runs/${id}`);
      state.selectedRun = run;
      state.builderTab = ACTIVE_STATES.has(run.state) ? "stream" : "diff";
      state.logOffset = 0;
      state.logAttempt = null;
      state.streamRaw = "";
      state.streamEntries = [];
      $("builder-artifact").textContent = "";
      $("builder-process").dataset.signature = "";
      renderBuilder();
      await loadBuilderTab();
      if (ACTIVE_STATES.has(run.state)) startBuilderPoll();
      else stopBuilderPoll();
    } catch (error) {
      flash(error.message, true);
    }
  }

  async function refreshSelectedRun() {
    if (!state.selectedRun) return;
    try {
      const run = await api(`/api/runs/${state.selectedRun.id}`);
      const changed = run.state !== state.selectedRun.state;
      state.selectedRun = run;
      const index = state.runs.findIndex((row) => row.id === run.id);
      if (index >= 0) state.runs[index] = run;
      if (changed) renderBuilder();
      else renderBuilderSession();
      if (TERMINAL_STATES.has(run.state)) stopBuilderPoll();
    } catch (error) {
      /* transient */
    }
  }

  async function cancelRun() {
    if (!state.selectedRun) return;
    try {
      await api(`/api/runs/${state.selectedRun.id}/cancel`, { method: "POST" });
      flash("Cancel requested.");
      await refreshSelectedRun();
      await loadRuns();
    } catch (error) {
      flash(error.message, true);
    }
  }

  function openTaskDialog(task) {
    state.taskPicked = task;
    $("task-title").textContent = task.title;
    $("task-meta").innerHTML = `
      <div>${esc(projectName(task.project))} · ${esc(task.task)} · ${esc(objectiveLabel(task))}</div>
      <div>${badge(task.status)} ${task.ready ? '<span class="badge st-shipped">ready</span>' : '<span class="badge st-stalled">blocked</span>'}</div>`;
    const reasons = [...(task.reasons || []), ...(task.warnings || [])];
    $("task-reasons").innerHTML = reasons.length
      ? reasons.map((reason) => `<div class="reason-line">${esc(reason.code)} — ${esc(reason.detail)}</div>`).join("")
      : '<div class="muted">No blocking reasons.</div>';
    const upModels = buildModels();
    const preferred = upModels.some((model) => model.id === state.workModel)
      ? state.workModel
      : upModels.find((model) => model.id === "flash")?.id || upModels[0]?.id || "";
    $("task-model-picker").innerHTML = upModels.length
      ? modelCards(upModels, "task_model", preferred)
      : '<span class="muted">No model available.</span>';
    const preferredReviewer = upModels.some((model) => model.id === state.workReviewer)
      ? state.workReviewer
      : state.roles?.reviewer || upModels[0]?.id || "";
    $("task-reviewer-picker").innerHTML = upModels.length
      ? modelCards(upModels, "task_reviewer", preferredReviewer)
      : '<span class="muted">No model available.</span>';
    $("task-run").disabled = !task.ready || upModels.length === 0;
    $("task-dialog").showModal();
  }

  async function startRun() {
    if (!state.taskPicked) return;
    const picked = $("task-model-picker").querySelector('input[name="task_model"]:checked');
    const model = picked?.value;
    const reviewerPicked = $("task-reviewer-picker").querySelector('input[name="task_reviewer"]:checked');
    const reviewer = reviewerPicked?.value || "";
    if (!model) { flash("Select a build model.", true); return; }
    try {
      localStorage.setItem("cc.model", model);
      state.workModel = model;
      const run = await api("/api/runs", {
        method: "POST",
        body: { task: state.taskPicked.task, model, reviewer: reviewer || null },
      });
      $("task-dialog").close();
      flash(`Started ${run.id}.`);
      state.runs = await api("/api/runs");
      renderBuilder();
      refreshStatus();
      go("builder");
      await selectRun(run.id);
    } catch (error) {
      const issues = error.payload?.issues || [];
      flash(issues.length ? issues.map((issue) => `${issue.code}: ${issue.detail}`).join("; ") : error.message, true);
    }
  }

  // ---------------------------------------------------------------- runs
  function sortRuns(runs) {
    const rank = (stateName) => {
      if (["running", "queued", "merging"].includes(stateName)) return 0;
      if (["gate_failed", "policy_failed", "review_changes", "blocked", "error"].includes(stateName)) return 1;
      return 2;
    };
    return [...runs].sort((a, b) => rank(a.state) - rank(b.state) || String(b.created).localeCompare(String(a.created)));
  }

  async function loadRuns() {
    state.runs = await api("/api/runs");
    renderBuilder();
    refreshStatus();
  }

  async function loadTasks() {
    state.tasks = (await api("/api/tasks")).tasks || [];
    renderBuilder();
    if (state.agenda) renderAgendaRows();
  }

  async function selectRun(id) {
    if (state.view !== "builder") go("builder");
    await selectBuilderRun(id);
  }

  // ---------------------------------------------------------------- events
  function connectSSE() {
    if (state.sse) state.sse.close();
    const source = new EventSource(`/api/events?token=${encodeURIComponent(state.token)}`);
    state.sse = source;
    source.onopen = () => {
      $("conn").textContent = "online";
      $("conn").className = "badge st-live";
    };
    source.onerror = () => {
      $("conn").textContent = "reconnecting";
      $("conn").className = "badge st-gated";
    };
    const names = [
      "run.started", "run.finished", "run.merged", "run.blocked",
      "gate.failed", "policy.failed", "review.verdict",
      "agenda.locked", "agenda.unlocked", "model.down", "shape.updated",
    ];
    names.forEach((name) => {
      source.addEventListener(name, async () => {
        try {
          if (name.startsWith("run.") || name === "review.verdict") {
            await Promise.all([loadRuns(), loadTasks()]);
            await refreshSelectedRun();
            if (state.agenda && !state.agendaDirty) await loadAgenda();
            if (state.view === "work") await refreshWork({ context: true });
          } else if (name === "shape.updated" && state.view === "work") {
            await refreshWork({ context: true });
          } else if (name.startsWith("agenda.") && state.agenda && !state.agendaDirty) {
            await loadAgenda();
          }
          refreshStatus();
          if (state.view === "overview") await loadOverview();
          if (state.view === "inquisitor") renderInquisitor();
        } catch (error) { /* ignore */ }
      });
    });
  }

  // ---------------------------------------------------------------- events / init
  function bind() {
    $("pin-form").addEventListener("submit", (event) => { event.preventDefault(); connect(); });
    $("logout").addEventListener("click", logout);
    $("settings-open").addEventListener("click", () => go("settings"));
    $("settings-save").addEventListener("click", saveSettings);
    $("settings-back").addEventListener("click", () => go("overview"));
    document.querySelectorAll("#nav button").forEach((button) => {
      button.addEventListener("click", () => go(button.dataset.view));
    });
    window.addEventListener("hashchange", () => {
      const raw = location.hash.replace(/^#/, "");
      const [base, taskPart] = raw.split("/");
      if (base === "work" && taskPart) {
        showWorkById(decodeURIComponent(taskPart));
        return;
      }
      if (base === "shape" && taskPart) {
        openShapePage(decodeURIComponent(taskPart));
        return;
      }
      showView(base || "overview");
    });

    $("objective-segments").addEventListener("click", (event) => {
      const button = event.target.closest("[data-horizon]");
      if (!button) return;
      state.objectiveHorizon = button.dataset.horizon;
      renderObjectives();
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-go]");
      if (button) go(button.dataset.go);
    });
    $("today-primary").addEventListener("click", () => {
      const action = $("today-primary").dataset.action;
      if (action === "lock") lockAgenda();
      else if (action === "suggest") suggestAgenda();
      else if (action === "queue") go("builder");
      else if (action === "run" && activeRuns()[0]) { go("builder"); selectRun(activeRuns()[0].id); }
    });
    $("today-secondary").addEventListener("click", () => {
      const action = $("today-secondary").dataset.action;
      if (action === "suggest") suggestAgenda();
      else if (action === "unlock") unlockAgenda();
    });
    $("agenda-date").addEventListener("change", () => {
      if (state.agendaDirty && !confirm("Discard unsaved agenda changes?")) {
        $("agenda-date").value = state.agendaDate;
        return;
      }
      loadAgenda();
    });
    $("agenda-save").addEventListener("click", saveAgenda);
    $("empty-suggest").addEventListener("click", suggestAgenda);
    $("focus-edit").addEventListener("click", openFocusDialog);
    $("focus-search").addEventListener("input", (event) => {
      state.focusSearch = event.target.value;
      renderFocusOptions();
    });
    $("focus-options").addEventListener("change", () => {
      const checked = document.querySelectorAll("#focus-options input:checked");
      if (checked.length > 3) {
        checked[checked.length - 1].checked = false;
        flash("Focus allows at most three projects.", true);
      }
      $("focus-count").textContent = `${document.querySelectorAll("#focus-options input:checked").length}/3`;
    });
    $("focus-save").addEventListener("click", saveFocusDialog);
    $("focus-slots").addEventListener("click", (event) => {
      const button = event.target.closest("[data-focus-remove]");
      if (button) removeFocus(button.dataset.focusRemove);
    });
    $("add-task").addEventListener("click", openCreateDialog);
    $("create-open").addEventListener("click", openCreateDialog);
    $("create-search").addEventListener("input", (event) => {
      state.createSearch = event.target.value;
      renderCreateTree();
    });
    $("create-project-save").addEventListener("click", createProject);
    $("create-task-save").addEventListener("click", createTaskFromForm);
    $("create-tree").addEventListener("click", (event) => {
      const projectAgenda = event.target.closest("[data-create-project-agenda]");
      if (projectAgenda) {
        addProjectToAgenda(projectAgenda.dataset.createProjectAgenda);
        return;
      }
      const objectiveAgenda = event.target.closest(
        "[data-create-objective-agenda]"
      );
      if (objectiveAgenda) {
        addObjectiveToAgenda(
          objectiveAgenda.dataset.project,
          objectiveAgenda.dataset.createObjectiveAgenda
        );
        return;
      }
      const taskAgenda = event.target.closest("[data-create-task-agenda]");
      if (taskAgenda) {
        addTasksToAgenda([
          {
            project: taskAgenda.dataset.project,
            objective: taskAgenda.dataset.objective,
            task: taskAgenda.dataset.createTaskAgenda,
          },
        ]);
        return;
      }
      const addObjective = event.target.closest("[data-create-add-objective]");
      if (addObjective) {
        const project = addObjective.dataset.createAddObjective;
        if (!state.createOpen.includes(project)) state.createOpen.push(project);
        state.createInline = { kind: "objective", project, objective: null };
        renderCreateTree();
        return;
      }
      const addTask = event.target.closest("[data-create-add-task]");
      if (addTask) {
        const project = addTask.dataset.project;
        if (!state.createOpen.includes(project)) state.createOpen.push(project);
        state.createInline = {
          kind: "task",
          project,
          objective: addTask.dataset.createAddTask,
        };
        renderCreateTree();
        return;
      }
      const save = event.target.closest("[data-create-inline-save]");
      if (save) {
        submitCreateInline();
        return;
      }
      const cancel = event.target.closest("[data-create-inline-cancel]");
      if (cancel) {
        state.createInline = null;
        renderCreateTree();
        return;
      }
      const objective = event.target.closest("[data-create-objective]");
      if (objective) {
        state.createParent = {
          project: objective.dataset.createProjectId,
          objective: objective.dataset.createObjective,
        };
        renderCreateTree();
        return;
      }
      const project = event.target.closest("[data-create-project]");
      if (project) {
        toggleCreateProject(project.dataset.createProject);
      }
    });
    $("create-tree").addEventListener("keydown", (event) => {
      if (event.target.id !== "create-inline-input") return;
      if (event.key === "Enter") {
        event.preventDefault();
        submitCreateInline();
      }
      if (event.key === "Escape") {
        state.createInline = null;
        renderCreateTree();
      }
    });
    $("agenda-list").addEventListener("input", (event) => {
      const input = event.target.closest("[data-reason]");
      if (!input || !state.agenda) return;
      state.agenda.items[Number(input.dataset.reason)].reason = input.value;
      markDirty();
    });
    $("agenda-list").addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-agenda-project]");
      if (toggle) {
        const project = toggle.dataset.agendaProject;
        const at = state.agendaOpen.indexOf(project);
        if (at >= 0) state.agendaOpen.splice(at, 1);
        else state.agendaOpen.push(project);
        renderAgendaRows();
        return;
      }
      const workButton = event.target.closest("[data-work-index]");
      if (workButton) {
        openWorkForAgenda(Number(workButton.dataset.workIndex));
        return;
      }
      const button = event.target.closest("[data-agenda-action]");
      if (!button || !state.agenda) return;
      const index = Number(button.dataset.index);
      const items = state.agenda.items;
      if (button.dataset.agendaAction === "remove") items.splice(index, 1);
      if (button.dataset.agendaAction === "up" && index > 0) {
        const [item] = items.splice(index, 1); items.splice(index - 1, 0, item);
      }
      if (button.dataset.agendaAction === "down" && index < items.length - 1) {
        const [item] = items.splice(index, 1); items.splice(index + 1, 0, item);
      }
      markDirty();
      renderAgendaRows();
    });

    $("overview-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-ov-action]");
      if (!button) return;
      const row = (state.overview || []).find((item) => item.id === button.dataset.ovRow);
      if (row) runOverviewAction(button.dataset.ovAction, row);
    });
    $("inq-list").addEventListener("click", (event) => {
      const open = event.target.closest("[data-inq-project]");
      if (open) {
        openShapePage(open.dataset.inqProject, {
          objective: open.dataset.inqObjective || null,
          task: open.dataset.inqTask,
        });
      }
    });
    $("shape-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-shape-action]");
      if (button) shapeAction(button.dataset.shapeAction);
    });
    $("shape-body").addEventListener("input", (event) => {
      if (!event.target.closest("[data-shape-q]")) return;
      growField(event.target);
      updateAnswerAction();
    });
    $("shape-refresh").addEventListener("click", () => loadShapePage());
    $("shape-send").addEventListener("click", sendShapeMessage);
    $("shape-mode").addEventListener("click", (event) => {
      const button = event.target.closest("[data-shape-mode]");
      if (!button) return;
      state.shapeMode = button.dataset.shapeMode;
      renderShapeSession();
    });
    $("shape-body").addEventListener("keydown", (event) => {
      const field = event.target.closest("[data-shape-q]");
      if (!field) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitAnswer(field);
      }
    });
    $("shape-prompt").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendShapeMessage();
      }
    });
    $("shape-tree-expand").addEventListener("click", () => {
      setCardFullscreen(
        "shape-tree-card",
        !$("shape-tree-card").classList.contains("fullscreen")
      );
    });
    $("shape-spec-generate").addEventListener("click", extrapolateModels);
    $("shape-spec-expand").addEventListener("click", () => {
      setCardFullscreen(
        "shape-spec-card",
        !$("shape-spec-card").classList.contains("fullscreen")
      );
    });
    $("shape-spec-tabs").addEventListener("click", (event) => {
      const add = event.target.closest('[data-spec-action="add-use-case"]');
      if (add) {
        addUseCase();
        return;
      }
      const tab = event.target.closest("[data-spec-tab]");
      if (!tab) return;
      state.specTab = tab.dataset.specTab;
      state.useStep = null;
      renderShapeSpec();
      const activeTab = $("shape-spec-tabs").querySelector(".spec-tab.active");
      if (activeTab && typeof activeTab.scrollIntoView === "function") {
        activeTab.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    });
    $("shape-use-editor").addEventListener("click", (event) => {
      const use = event.target.closest("[data-use-action]");
      if (use) {
        useAction(use.dataset.useAction, use.dataset.branch);
        return;
      }
      const c4 = event.target.closest("[data-c4-action]");
      if (c4) c4Action(c4.dataset.c4Action, c4.dataset.rel);
    });
    $("shape-use-editor").addEventListener("change", (event) => {
      if (event.target.id === "step-kind") useAction("kind");
      if (event.target.id === "c4-type") c4Action("retype");
    });
    $("shape-use-editor").addEventListener("input", () => {
      if ($("uc-title")) state.useDirty = true;
      if ($("c4-name")) state.c4Dirty = true;
    });
    $("shape-diff-btn").addEventListener("click", () => shapeAction("diff"));
    $("shape-check").addEventListener("click", () => shapeAction("check"));
    $("shape-cancel").addEventListener("click", () => shapeAction("cancel"));
    $("shape-approve").addEventListener("click", () => shapeAction("approve"));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeFullscreen();
    });
    $("shape-tree-editor").addEventListener("click", (event) => {
      const button = event.target.closest("[data-tree-action]");
      if (!button) return;
      if (button.dataset.treeAction === "save") {
        readTreeEditor();
        saveShapeTree();
      } else if (button.dataset.treeAction === "add") {
        addTreeTask();
      } else if (button.dataset.treeAction === "delete") {
        deleteTreeTask();
      }
    });
    $("builder-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-builder-tab]");
      if (!button) return;
      state.builderTab = button.dataset.builderTab;
      renderBuilderSession();
      loadBuilderTab();
    });
    $("builder-decomp").addEventListener("click", (event) => {
      const card = event.target.closest("[data-decomp-task]");
      if (!card) return;
      const run = builderRunFor(card.dataset.decompTask);
      if (run) selectBuilderRun(run.id);
      else showWorkById(card.dataset.decompTask);
    });
    $("builder-spec-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-builder-spec-tab]");
      if (!button) return;
      state.builderSpecTab = button.dataset.builderSpecTab;
      renderBuilderSpec(builderProject());
    });
    $("builder-cancel").addEventListener("click", cancelRun);
    $("builder-build").addEventListener("click", toggleProjectBuild);
    $("builder-work").addEventListener("click", () => {
      if (state.selectedRun) showWorkById(state.selectedRun.task);
    });
    $("builder-follow").addEventListener("click", () => {
      state.builderFollow = !state.builderFollow;
      renderBuilderSession();
      if (state.builderFollow) {
        $("builder-stream").scrollTop = $("builder-stream").scrollHeight;
      }
    });
    $("task-run").addEventListener("click", startRun);

    $("status-strip").addEventListener("click", (event) => {
      const button = event.target.closest("[data-status]");
      if (!button) return;
      go("builder");
      const target = button.dataset.status === "running"
        ? activeRuns()[0]
        : state.runs.find((run) => run.state === "blocked");
      if (target) selectRun(target.id);
    });

    document.querySelectorAll("dialog [data-close]").forEach((button) => {
      button.addEventListener("click", () => $(button.dataset.close).close());
    });
    $("view-work").addEventListener("click", (event) => {
      const button = event.target.closest("[data-work-action]");
      if (!button || !state.workTask) return;
      workAction(button.dataset.workAction, button.dataset.runId || null);
    });
    $("work-back").addEventListener("click", () => go("objectives"));
    $("work-model-picker").addEventListener("change", (event) => {
      if (event.target.name !== "work_model") return;
      state.workModel = event.target.value;
      localStorage.setItem("cc.model", state.workModel);
      renderWork();
    });
    $("work-reviewer-picker").addEventListener("change", (event) => {
      if (event.target.name !== "work_reviewer") return;
      state.workReviewer = event.target.value;
      localStorage.setItem("cc.reviewer", state.workReviewer);
      renderWork();
    });
    $("shape-model-picker").addEventListener("change", async (event) => {
      if (event.target.name !== "shape_model") return;
      const project = state.shapeProject;
      if (!project) return;
      try {
        await api(`/api/shapes/${encodeURIComponent(project)}`, {
          method: "PATCH",
          body: { model: event.target.value },
        });
        flash(`Inquisitor model set to ${modelLabel(event.target.value)}.`);
        await loadShapePage();
      } catch (error) {
        flash(error.message, true);
        await loadShapePage();
      }
    });
    document.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });
  }

  function init() {
    bind();
    $("agenda-date").value = todayISO();
    if (state.token) {
      boot().catch((error) => showLogin(error.message));
    } else {
      showLogin("Enter the PIN to start.");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
