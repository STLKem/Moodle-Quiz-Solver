(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    config: null,
    catalog: [],
    status: null,
    stream: null,
    dashSource: "watch",
    selectedLog: null,
  };

  const titles = {
    dashboard: ["Dashboard", "Overview of services and quick actions."],
    solve: ["Solve", "Run a one-off quiz solve and watch the live log."],
    services: ["Services", "Start or stop watch, bots, and chat workers."],
    settings: ["Settings", "Keys, Moodle credentials, agent toggles, solver options."],
    history: ["History", "Past attempt folders and summaries from logs/."],
    logs: ["Logs", "Browse remote_runs output files."],
  };

  function toast(msg) {
    const el = $("#toast");
    el.hidden = false;
    el.textContent = msg;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 3200);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.message || `HTTP ${res.status}`);
    }
    return data;
  }

  function setView(name) {
    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    const [title, lede] = titles[name] || [name, ""];
    $("#viewTitle").textContent = title;
    $("#viewLede").textContent = lede;
    if (name === "history") loadHistory();
    if (name === "logs") loadLogs();
    if (name === "settings") fillSettings();
  }

  function fmtStatus(obj) {
    if (!obj) return "—";
    return obj.running ? `Running · pid ${obj.pid || "?"}` : "Stopped";
  }

  async function refreshStatus() {
    const st = await api("/api/status");
    state.status = st;
    $("#statWatch").textContent = st.watch.running ? "Running" : "Stopped";
    $("#statBots").textContent = st.bots.running ? "Running" : "Stopped";
    $("#statChat").textContent = st.chat.running ? "Running" : "Stopped";
    $("#statAgents").textContent = String(st.agent_count ?? "—");
    $("#svcWatch").textContent = fmtStatus(st.watch);
    $("#svcBots").textContent = fmtStatus(st.bots);
    $("#svcChat").textContent = fmtStatus(st.chat);
    $("#configPath").textContent = st.config_path || "config.yaml";
    const pill = $("#busyPill");
    if (st.solve_busy || st.watch.running || st.bots.running || st.chat.running) {
      pill.textContent = st.solve_busy ? "Solve running" : "Services active";
      pill.classList.add("busy");
    } else {
      pill.textContent = "Idle";
      pill.classList.remove("busy");
    }
  }

  async function refreshConfig() {
    const data = await api("/api/config");
    state.config = data.config || {};
    state.catalog = data.agents_catalog || [];
    fillSettings();
  }

  const KEY_FIELDS = [
    ["key_groq", "groq_api_key"],
    ["key_cerebras", "cerebras_api_key"],
    ["key_google", "google_api_key"],
    ["key_deepseek", "deepseek_api_key"],
    ["key_openrouter", "openrouter_api_key"],
    ["key_github", "github_api_key"],
  ];

  function joinIds(arr) {
    return (arr || []).map(String).join(", ");
  }

  function readModelList(containerId) {
    return $$(`#${containerId} .model-row`).map((row) => ({
      model: $("input[type=text]", row).value.trim(),
      enabled: $("input[type=checkbox]", row).checked,
    })).filter((x) => x.model);
  }

  function renderModelList(containerId, entries) {
    const box = $(`#${containerId}`);
    box.innerHTML = "";
    const list = entries && entries.length ? entries : [];
    for (const entry of list) {
      const row = document.createElement("div");
      row.className = "model-row";
      row.innerHTML = `
        <input type="text" value="${String(entry.model || "").replace(/"/g, "&quot;")}" placeholder="model id" />
        <label class="switch" title="Enabled">
          <input type="checkbox" ${entry.enabled ? "checked" : ""} />
          <i></i>
        </label>
        <button type="button" class="btn remove" title="Remove">✕</button>`;
      $(".remove", row).onclick = () => row.remove();
      box.appendChild(row);
    }
  }

  function addModelRow(containerId, model = "") {
    const box = $(`#${containerId}`);
    const row = document.createElement("div");
    row.className = "model-row";
    row.innerHTML = `
      <input type="text" value="${model}" placeholder="provider/model:tag" />
      <label class="switch" title="Enabled">
        <input type="checkbox" checked />
        <i></i>
      </label>
      <button type="button" class="btn remove" title="Remove">✕</button>`;
    $(".remove", row).onclick = () => row.remove();
    box.appendChild(row);
    $("input[type=text]", row).focus();
  }

  function fillSettings() {
    const cfg = state.config || {};
    const moodle = cfg.moodle || {};
    const agents = cfg.agents || {};
    const solver = cfg.solver || {};
    const monitor = cfg.monitor || {};
    const remote = cfg.remote || {};
    const chat = cfg.chat_bot || {};

    $("#moodle_url").value = moodle.url || "";
    $("#moodle_user").value = moodle.username || "";
    $("#moodle_pass").value = "";
    $("#moodle_pass").placeholder = moodle.password__masked || "••••••••";
    $("#moodle_mode").value = moodle.preferred_mode || "auto";

    for (const [id, field] of KEY_FIELDS) {
      const el = $(`#${id}`);
      el.value = "";
      el.placeholder = agents[`${field}__masked`] || "Paste key…";
    }

    $("#calls_per_agent").value = agents.calls_per_agent ?? 1;
    $("#timeout_seconds").value = agents.timeout_seconds ?? 45;
    $("#temperature_low").value = agents.temperature_low ?? 0.2;
    $("#temperature_high").value = agents.temperature_high ?? 0.7;

    $("#auto_finish").checked = !!solver.auto_finish;
    $("#save_log").checked = solver.save_log !== false;
    $("#show_reasoning").checked = !!solver.show_reasoning;
    $("#question_delay").value = solver.question_delay ?? 0;
    $("#defer_retries").value = solver.defer_retries ?? 1;
    $("#log_file").value = solver.log_file || "quiz_log.json";
    $("#logs_dir").value = solver.logs_dir || "logs";

    $("#poll_seconds").value = monitor.poll_seconds ?? 60;
    $("#poll_jitter").value = monitor.poll_jitter_seconds ?? 10;
    $("#state_file").value = monitor.state_file || "monitor_state.json";
    $("#exclude_quiz_ids").value = joinIds(monitor.exclude_quiz_ids);

    $("#tg_token").value = "";
    $("#tg_token").placeholder = remote.telegram_bot_token__masked || "Telegram token";
    $("#dc_token").value = "";
    $("#dc_token").placeholder = remote.discord_bot_token__masked || "Discord token";
    $("#runs_dir").value = remote.runs_dir || "remote_runs";
    $("#tg_users").value = joinIds(remote.allowed_telegram_user_ids);
    $("#dc_users").value = joinIds(remote.allowed_discord_user_ids);

    $("#chat_model").value = chat.model || "";
    $("#chat_conv").value = chat.conversation_id || 0;
    $("#chat_poll").value = chat.poll_seconds ?? 10;
    $("#chat_debug").checked = !!chat.debug;
    $("#chat_auto").checked = chat.auto_start_with_remote_bots !== false;
    $("#chat_rules").value = chat.reply_rules || "";

    renderModelList("deepseekList", agents.deepseek_models || []);
    renderModelList("openrouterList", agents.openrouter_models || []);

    const box = $("#agentToggles");
    box.innerHTML = "";
    const builtin = (state.catalog || []).filter(
      (row) => row.group === "agents" || row.kind === "builtin" || row.kind === "legacy"
    );
    for (const row of builtin) {
      const label = document.createElement("label");
      label.className = "toggle";
      const title = row.label || row.id;
      label.innerHTML = `
        <span title="${row.id}">${title}</span>
        <span class="switch">
          <input type="checkbox" data-agent="${row.id}" ${row.enabled ? "checked" : ""} />
          <i></i>
        </span>`;
      box.appendChild(label);
    }

    box.onchange = async (e) => {
      const input = e.target;
      if (!(input instanceof HTMLInputElement) || !input.dataset.agent) return;
      try {
        const data = await api("/api/agents/toggle", {
          method: "POST",
          body: JSON.stringify({ id: input.dataset.agent, enabled: input.checked }),
        });
        state.catalog = data.agents_catalog || state.catalog;
        toast(`${input.dataset.agent}: ${input.checked ? "on" : "off"}`);
        refreshStatus();
      } catch (err) {
        toast(String(err.message || err));
        input.checked = !input.checked;
      }
    };
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const agents = {
      calls_per_agent: Number($("#calls_per_agent").value || 1),
      timeout_seconds: Number($("#timeout_seconds").value || 45),
      temperature_low: Number($("#temperature_low").value || 0.2),
      temperature_high: Number($("#temperature_high").value || 0.7),
      deepseek_models: readModelList("deepseekList"),
      openrouter_models: readModelList("openrouterList"),
      enabled: {},
    };
    for (const [id, field] of KEY_FIELDS) {
      const val = $(`#${id}`).value.trim();
      if (val) agents[field] = val;
    }
    $$("#agentToggles input[data-agent]").forEach((input) => {
      agents.enabled[input.dataset.agent] = input.checked;
    });

    const body = {
      moodle: {
        url: $("#moodle_url").value.trim(),
        username: $("#moodle_user").value.trim(),
        preferred_mode: $("#moodle_mode").value,
      },
      agents,
      solver: {
        auto_finish: $("#auto_finish").checked,
        save_log: $("#save_log").checked,
        show_reasoning: $("#show_reasoning").checked,
        question_delay: Number($("#question_delay").value || 0),
        defer_retries: Number($("#defer_retries").value || 0),
        log_file: $("#log_file").value.trim() || "quiz_log.json",
        logs_dir: $("#logs_dir").value.trim() || "logs",
      },
      monitor: {
        poll_seconds: Number($("#poll_seconds").value || 60),
        poll_jitter_seconds: Number($("#poll_jitter").value || 0),
        state_file: $("#state_file").value.trim() || "monitor_state.json",
        exclude_quiz_ids: $("#exclude_quiz_ids").value,
      },
      remote: {
        runs_dir: $("#runs_dir").value.trim() || "remote_runs",
        allowed_telegram_user_ids: $("#tg_users").value,
        allowed_discord_user_ids: $("#dc_users").value,
      },
      chat_bot: {
        model: $("#chat_model").value.trim(),
        conversation_id: Number($("#chat_conv").value || 0),
        poll_seconds: Number($("#chat_poll").value || 10),
        debug: $("#chat_debug").checked,
        auto_start_with_remote_bots: $("#chat_auto").checked,
        reply_rules: $("#chat_rules").value,
      },
    };
    const pass = $("#moodle_pass").value;
    if (pass) body.moodle.password = pass;
    const tg = $("#tg_token").value.trim();
    const dc = $("#dc_token").value.trim();
    if (tg) body.remote.telegram_bot_token = tg;
    if (dc) body.remote.discord_bot_token = dc;

    try {
      const data = await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
      state.config = data.config;
      state.catalog = data.agents_catalog || state.catalog;
      fillSettings();
      $("#saveMsg").textContent = "Saved.";
      toast("Settings saved");
      refreshStatus();
    } catch (err) {
      $("#saveMsg").textContent = String(err.message || err);
      toast(String(err.message || err));
    }
  }

  async function startSolve(view) {
    const data = await api("/api/solve", {
      method: "POST",
      body: JSON.stringify({ view }),
    });
    toast(`Solve started for view ${data.view_id}`);
    openStream("solve", $("#solveConsole"));
    openStream(state.dashSource, $("#dashConsole"));
    refreshStatus();
  }

  function stopStream() {
    if (state.stream) {
      state.stream.abort();
      state.stream = null;
    }
  }

  async function openStream(source, pre, name = "") {
    stopStream();
    const ctrl = new AbortController();
    state.stream = ctrl;
    pre.textContent = "";
    const qs = name
      ? `name=${encodeURIComponent(name)}`
      : `source=${encodeURIComponent(source)}`;
    try {
      const res = await fetch(`/api/logs/stream?${qs}`, { signal: ctrl.signal });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        pre.textContent += decoder.decode(value, { stream: true });
        pre.scrollTop = pre.scrollHeight;
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        pre.textContent += `\n[stream error] ${err.message || err}`;
      }
    }
  }

  async function loadHistory() {
    const data = await api("/api/history");
    const list = $("#historyList");
    const attempts = data.attempts || [];
    if (!attempts.length) {
      list.innerHTML = `<div class="muted">No attempts in logs/ yet.</div>`;
      return;
    }
    list.innerHTML = "";
    for (const a of attempts) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "list-item";
      btn.innerHTML = `
        <div class="title">${a.id}</div>
        <div class="meta">${a.mtime} · ${a.question_count} questions</div>`;
      btn.onclick = async () => {
        $$(".list-item", list).forEach((x) => x.classList.remove("active"));
        btn.classList.add("active");
        const detail = await api(`/api/history/${encodeURIComponent(a.id)}`);
        const text = detail.summary_text
          || JSON.stringify(detail.summary || {}, null, 2)
          || "No summary.";
        const qlines = (detail.questions || [])
          .map((q) => {
            const vote = q.vote ? ` → ${JSON.stringify(q.vote.winner ?? q.vote)}` : "";
            return `${q.id}${vote}`;
          })
          .join("\n");
        $("#historyDetail").textContent = `${text}\n\nQuestions:\n${qlines || "(none)"}`;
      };
      list.appendChild(btn);
    }
  }

  async function loadLogs() {
    const data = await api("/api/logs");
    const list = $("#logsList");
    const logs = data.logs || [];
    if (!logs.length) {
      list.innerHTML = `<div class="muted">No files in remote_runs/ yet.</div>`;
      return;
    }
    list.innerHTML = "";
    for (const log of logs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "list-item";
      btn.innerHTML = `
        <div class="title">${log.name}</div>
        <div class="meta">${log.kind} · ${log.mtime} · ${Math.round(log.size / 1024)} KB</div>`;
      btn.onclick = async () => {
        $$(".list-item", list).forEach((x) => x.classList.remove("active"));
        btn.classList.add("active");
        state.selectedLog = log.name;
        $("#logTitle").textContent = log.name;
        const content = await api(`/api/logs/content?name=${encodeURIComponent(log.name)}&tail=500`);
        $("#logsConsole").textContent = (content.lines || []).join("\n") || "(empty)";
      };
      list.appendChild(btn);
    }
  }

  function wire() {
    $$(".nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => setView(btn.dataset.view));
    });
    $("#refreshBtn").onclick = async () => {
      await Promise.all([refreshStatus(), refreshConfig()]);
      toast("Refreshed");
    };
    $("#settingsForm").addEventListener("submit", saveSettings);
    $("#addDeepseek").onclick = () => addModelRow("deepseekList", "deepseek-chat");
    $("#addOpenrouter").onclick = () => addModelRow("openrouterList", "");
    $("#quickSolveForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await startSolve($("#quickView").value.trim());
      } catch (err) {
        toast(String(err.message || err));
      }
    });
    $("#solveForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await startSolve($("#solveView").value.trim());
      } catch (err) {
        toast(String(err.message || err));
      }
    });

    const actions = [
      ["watchStart", "/api/watch/start", "Watch started"],
      ["watchStop", "/api/watch/stop", "Watch stopped"],
      ["botsStart", "/api/bots/start", "Bots started"],
      ["botsStop", "/api/bots/stop", "Bots stopped"],
      ["chatStart", "/api/chat/start", "Chat bot started"],
      ["chatStop", "/api/chat/stop", "Chat bot stopped"],
    ];
    for (const [id, path, msg] of actions) {
      $(`#${id}`).onclick = async () => {
        try {
          await api(path, { method: "POST", body: "{}" });
          toast(msg);
          await refreshStatus();
          if (id === "watchStart") openStream("watch", $("#dashConsole"));
          if (id === "botsStart") openStream("bots", $("#dashConsole"));
        } catch (err) {
          toast(String(err.message || err));
        }
      };
    }

    $$("#dashLogSource button").forEach((btn) => {
      btn.onclick = () => {
        $$("#dashLogSource button").forEach((b) => b.classList.toggle("on", b === btn));
        state.dashSource = btn.dataset.source;
        openStream(state.dashSource, $("#dashConsole"));
      };
    });

    $("#followLog").onclick = () => {
      if (!state.selectedLog) {
        toast("Select a log first");
        return;
      }
      openStream("other", $("#logsConsole"), state.selectedLog);
    };
  }

  async function boot() {
    wire();
    setView("dashboard");
    try {
      await Promise.all([refreshStatus(), refreshConfig()]);
      openStream("watch", $("#dashConsole"));
      setInterval(refreshStatus, 4000);
    } catch (err) {
      toast(`Boot failed: ${err.message || err}`);
    }
  }

  boot();
})();
