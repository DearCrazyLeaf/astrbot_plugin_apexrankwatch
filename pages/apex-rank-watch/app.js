(() => {
  "use strict";
  const bridge = window.AstrBotPluginView;
  const $ = (id) => document.getElementById(id);
  const state = { dashboard: null, sessions: [], values: {}, schema: {}, secrets: {}, groups: [], entries: [], dirty: false, ready: false, busy: false, filter: "all", logUmo: "", logRequest: 0 };
  const titles = { overview: "运行概览", sessions: "会话订阅", logs: "推送日志", bindings: "个人查询绑定", settings: "插件配置" };
  const sections = { overview: "OVERVIEW", sessions: "SESSIONS", logs: "DELIVERY LOG", bindings: "QUERY BINDINGS", settings: "CONFIGURATION" };
  const labels = { sent: "发送成功", failed: "发送失败", recorded: "仅记录", fetch_failed: "取数失败" };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const get = async (path) => { if (!bridge?.apiGet) throw new Error("请从 AstrBot 插件页面打开 Dashboard"); const result = await bridge.apiGet(path); return result?.data ?? result; };
  const post = async (path, body) => { if (!bridge?.apiPost) throw new Error("页面桥接未就绪"); const result = await bridge.apiPost(path, body); return result?.data ?? result; };
  const time = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "暂无";
  let noticeTimer;
  function notice(message, error = false) { clearTimeout(noticeTimer); $("notice").textContent = message; $("notice").className = `notice show${error ? " error" : ""}`; noticeTimer = setTimeout(() => { $("notice").className = "notice"; }, 6500); }
  function dirty(value = true) { state.dirty = value; $("dirty-indicator").hidden = !value; }
  function openPage(name) {
    document.querySelectorAll(".page").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === name));
    $("page-title").textContent = titles[name] || titles.overview;
    $("page-section").textContent = sections[name] || sections.overview;
    if (name === "logs") void loadArchive();
  }
  function showOverview(data) {
    state.dashboard = data;
    const runtime = data.runtime || {};
    const status = { waiting: "等待首次轮询", running: "轮询中", success: "轮询完成", failed: "轮询失败" }[runtime.status] || "等待首次轮询";
    const pill = document.querySelector(".status-pill"); pill.textContent = status; pill.className = `status-pill ${runtime.status || "waiting"}`;
    document.querySelector(".api-status").textContent = data.api_key_set ? "API KEY 已配置" : "API KEY 未配置";
    const interval = Number(data.check_interval || 2);
    $("overview-poll").textContent = status;
    $("overview-poll-hint").textContent = runtime.last_error || `上次完成：${time(runtime.last_finished)}`;
    $("overview-api").textContent = data.api_key_set ? "已连接" : "未配置";
    $("overview-interval").textContent = `${interval} 分钟`;
    $("overview-interval-hint").textContent = `配置中的 API 请求超时：${Number(data.timeout_ms || 10000).toLocaleString("zh-CN")} 毫秒`;
    $("metric-sessions").textContent = `${data.enabled_session_count ?? 0} / ${data.session_count ?? 0}`;
    $("metric-notify").textContent = data.notify_count ?? 0;
    $("metric-players").textContent = data.player_count ?? 0;
    $("metric-record").textContent = data.record_count ?? 0;
    const recent = data.recent_entries || [];
    $("recent-entries").innerHTML = recent.length ? recent.map((entry) => `<tr><td class="time-cell">${esc(time(entry.created_at))}</td><td><span class="table-player"><img src="${esc(data.rank_icons?.[entry.rank_icon] || data.rank_icons?.["master.png"] || "")}" alt="" /><strong>${esc(entry.player)}</strong></span></td><td class="umo-cell" title="${esc(entry.umo)}">${esc(entry.umo)}</td><td class="score-cell ${entry.score_delta > 0 ? "positive" : entry.score_delta < 0 ? "negative" : "neutral"}">${entry.score_delta == null ? "—" : `${entry.score_delta > 0 ? "+" : ""}${entry.score_delta} RP`}</td><td><span class="result-label ${entry.status}">${esc(labels[entry.status] || entry.status)}</span></td></tr>`).join("") : '<tr><td colspan="5" class="empty-state">暂无推送记录。</td></tr>';
    const sessions = data.sessions || [];
    $("session-overview").innerHTML = sessions.length ? sessions.slice(0, 6).map((session) => `<div class="overview-session"><span class="session-state-mark ${session.allowed === false ? "restricted" : session.enabled ? "online" : "offline"}"></span><div><strong>${esc(session.label || session.group_id || session.umo)}</strong><code>${esc(session.umo)}</code></div><span class="overview-session-count">${(session.players || []).length} 人</span></div>`).join("") : '<p class="empty-state">暂无会话记录。</p>';
  }
  function active(session) { return session.enabled && session.allowed !== false; }
  function renderSessions() {
    const host = $("session-cards"); host.replaceChildren();
    if (!state.sessions.length) host.innerHTML = '<div class="panel empty-state">还没有订阅会话。可手动新增完整 UMO，也可在群内用原有 /apex监控 或 /持续记录 添加玩家。</div>';
    state.sessions.forEach((session, index) => {
      const subscribed = new Map((session.subscriptions || []).map((item) => [item.player_key, item.watch_mode]));
      const card = document.createElement("article");
      card.className = `session-card${active(session) ? "" : " paused"}`;
      card.dataset.index = index;
      card.dataset.state = active(session) ? "enabled" : "paused";
      card.dataset.search = `${session.label} ${session.umo} ${(session.available_players || []).map((item) => item.name).join(" ")}`.toLowerCase();
      const rows = (session.available_players || []).map((player, i) => `<div class="subscription-row${player.blocked ? " blocked" : ""}"><label><input type="checkbox" data-player="${i}" ${subscribed.has(player.key) ? "checked" : ""} ${player.blocked && !subscribed.has(player.key) ? "disabled" : ""} /><img src="${esc(state.dashboard?.rank_icons?.[player.rank_icon] || state.dashboard?.rank_icons?.["master.png"] || "")}" alt="" /><span><strong>${esc(player.name)}</strong><small>${esc(player.platform)} · ${esc(player.rank)} · ${player.score} RP${player.blocked ? " · 全局黑名单" : ""}</small></span></label><select data-mode="${i}" aria-label="${esc(player.name)} 的订阅模式"><option value="notify" ${subscribed.get(player.key) !== "record" ? "selected" : ""}>通报+记录</option><option value="record" ${subscribed.get(player.key) === "record" ? "selected" : ""}>仅记录</option></select></div>`).join("");
      card.innerHTML = `<div class="session-head"><div class="session-title"><span class="session-number">${String(index + 1).padStart(2, "0")}</span><div><h3>${esc(session.label || "新会话")}</h3><code>${esc(session.umo || "填写完整 UMO 后选择玩家")}</code></div></div><label class="switch" title="自动监控与推送"><input data-enabled type="checkbox" ${session.enabled ? "checked" : ""} aria-label="启用本会话监控与推送" /><span></span></label></div><div class="session-meta"><span class="tag ${active(session) ? "live" : "paused-tag"}">${session.allowed === false ? "白名单限制" : session.enabled ? "● 已启用" : "○ 已暂停"}</span><span data-count>${subscribed.size} 名订阅玩家</span><span>${session.configured ? "已配置会话" : "群内发现"}</span></div>${session.blocked_reason ? `<p class="session-warning">${esc(session.blocked_reason)}</p>` : ""}<div class="session-fields"><label>备注<input data-label maxlength="80" value="${esc(session.label || "")}" placeholder="例如：Apex 车队" /></label><label>推送格式<select data-output><option value="inherit">跟随全局</option><option value="image">图片卡片</option><option value="text">文字消息</option></select></label></div><label class="field umo-field">完整 UMO<input data-umo ${session._new ? "" : "readonly"} value="${esc(session.umo || "")}" placeholder="qq-a:GroupMessage:10001" /></label><label class="check-row session-season-toggle"><input data-season-keyword type="checkbox" ${session.season_keyword_enabled !== false ? "checked" : ""} aria-label="启用本会话赛季关键词自动回复" /> 启用本会话赛季关键词自动回复<small>仅影响普通消息包含“赛季”的自动回复</small></label><div class="editor-heading"><strong>玩家订阅</strong><small>勾选加入 / 取消勾选退订</small></div><div class="subscription-list">${rows || '<p class="empty-state">暂无监控玩家，输入玩家目标后添加。</p>'}</div><form class="add-player"><label>添加新的监控玩家<input name="target" required placeholder="玩家名或 uid:1234" maxlength="200" /></label><select name="platform" aria-label="玩家平台"><option value="">自动平台</option><option value="PC">PC</option><option value="PS4">PlayStation</option><option value="X1">Xbox</option><option value="SWITCH">Switch</option></select><button class="primary" type="submit">添加并订阅</button></form><div class="session-foot"><button class="text-button" data-clear>清空订阅并暂停</button><button class="text-button" data-log>此会话推送日志 →</button></div>`;
      card.querySelector("[data-output]").value = session.output_mode || "inherit";
      card.querySelector("[data-enabled]").addEventListener("change", (event) => { session.enabled = event.target.checked; dirty(); });
      card.querySelector("[data-label]").addEventListener("input", (event) => { session.label = event.target.value; dirty(); });
      card.querySelector("[data-output]").addEventListener("change", (event) => { session.output_mode = event.target.value; dirty(); });
      card.querySelector("[data-season-keyword]").addEventListener("change", (event) => { session.season_keyword_enabled = event.target.checked; dirty(); });
      card.querySelector("[data-umo]").addEventListener("input", (event) => {
        session.umo = event.target.value.trim(); session.group_id = session.umo.split(":").slice(2).join(":");
        const existing = state.sessions.find((item) => item !== session && item.group_id === session.group_id);
        if (existing) session.available_players = structuredClone(existing.available_players || []);
        dirty();
      });
      card.querySelector("[data-umo]").addEventListener("change", () => {
        if (session._new && /^[^:\s]+:GroupMessage:.+$/.test(session.umo)) renderSessions();
      });
      const collect = () => {
        session.subscriptions = [...card.querySelectorAll("[data-player]:checked")].map((input) => ({ player_key: session.available_players[Number(input.dataset.player)].key, watch_mode: card.querySelector(`[data-mode="${input.dataset.player}"]`).value }));
        card.querySelector("[data-count]").textContent = `${session.subscriptions.length} 名订阅玩家`; dirty();
      };
      card.querySelectorAll("[data-player], [data-mode]").forEach((input) => input.addEventListener("change", collect));
      card.querySelector("[data-clear]").addEventListener("click", () => { session.subscriptions = []; session.enabled = false; dirty(); renderSessions(); });
      card.querySelector("[data-log]").addEventListener("click", () => { state.logUmo = session.umo; openPage("logs"); });
      card.querySelector(".add-player").addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const target = form.elements.target.value.trim(); const platform = form.elements.platform.value;
        const umo = session.umo;
        if (!await saveAll(false)) return;
        form.querySelector("button").disabled = true;
        try { await post("/subscriptions", { umo, target, platform, watch_mode: "notify" }); await loadAll(); notice("已添加玩家订阅；当前分数已建立基线，之后的变化会推送到此 UMO"); }
        catch (error) { notice(`订阅失败：${error.message}`, true); }
        finally { form.querySelector("button").disabled = false; }
      });
      host.append(card);
    });
    const tabs = [...document.querySelectorAll("[data-filter]")];
    tabs[0].innerHTML = `全部 <b>${state.sessions.length}</b>`;
    tabs[1].innerHTML = `可推送 <b>${state.sessions.filter(active).length}</b>`;
    tabs[2].innerHTML = `暂停 / 受限 <b>${state.sessions.filter((s) => !active(s)).length}</b>`;
    filterSessions();
  }
  function filterSessions() {
    const query = $("session-search").value.trim().toLowerCase();
    document.querySelectorAll(".session-card").forEach((card) => { card.hidden = (state.filter !== "all" && card.dataset.state !== state.filter) || (query && !card.dataset.search.includes(query)); });
  }
  const settingGroups = [
    { title: "基础与请求", keys: ["api_key", "check_interval", "timeout_ms", "max_retries", "min_valid_score", "debug_logging", "allow_private"] },
    { title: "消息与归档", keys: ["output_mode", "reply_to_command", "push_archive_enabled", "push_archive_retention_days"] },
    { title: "用户与访问控制", keys: ["owner_qq", "user_blacklist", "whitelist_enabled", "whitelist_groups", "blacklist", "query_blocklist", "alias_enabled", "alias_admin_only", "player_aliases"] },
    { title: "字体与数据", keys: ["font_auto_download", "font_download_url", "season_probe_player", "data_dir"] },
  ];
  const multilineSettings = new Set(["owner_qq", "user_blacklist", "whitelist_groups", "blacklist", "query_blocklist", "player_aliases"]);
  function settingField(key, item) {
    const description = esc(item.description || key);
    const hint = item.hint ? `<small class="field-hint">${esc(item.hint)}</small>` : "";
    if (item.type === "bool") return `<label class="check-row"><input data-config="${esc(key)}" type="checkbox" /> ${description}${hint}</label>`;
    let control;
    if (Array.isArray(item.options) && item.options.length) {
      control = `<select data-config="${esc(key)}">${item.options.map((option) => { const value = typeof option === "object" ? option.value : option; const label = typeof option === "object" ? option.label : option; return `<option value="${esc(value)}">${esc(label)}</option>`; }).join("")}</select>`;
    } else if (item.type === "int") {
      control = `<input data-config="${esc(key)}" type="number"${item.min != null ? ` min="${Number(item.min)}"` : ""}${item.max != null ? ` max="${Number(item.max)}"` : ""} />`;
    } else if (key === "api_key" || key === "font_download_url") {
      control = `<input data-secret="${esc(key)}" type="password" autocomplete="new-password" placeholder="留空保持现有值" />`;
    } else if (multilineSettings.has(key)) {
      control = `<textarea data-config="${esc(key)}" rows="3" placeholder="多个值用逗号分隔"></textarea>`;
    } else {
      control = `<input data-config="${esc(key)}" type="text" />`;
    }
    const secretHint = key === "api_key" || key === "font_download_url" ? `<small class="field-hint">${state.secrets[key] ? "已设置；留空保持当前值。" : "尚未设置。"}</small>` : "";
    return `<label class="field">${description}${control}${secretHint}${hint}</label>`;
  }
  function fillSettings(data) {
    state.values = { ...(data.values || {}) };
    state.schema = data.schema || {};
    state.secrets = data.secrets || {};
    const fields = Object.entries(state.schema).filter(([key, item]) => key !== "session_settings" && ["bool", "int", "string"].includes(item?.type));
    const assigned = new Set();
    const groups = settingGroups.map((group) => {
      const keys = group.keys.filter((key) => state.schema[key]);
      keys.forEach((key) => assigned.add(key));
      return { title: group.title, keys };
    }).filter((group) => group.keys.length);
    const extra = fields.map(([key]) => key).filter((key) => !assigned.has(key));
    if (extra.length) groups.push({ title: "其他配置", keys: extra });
    $("settings-grid").innerHTML = groups.map((group) => `<section class="panel settings-panel"><div class="panel-heading"><h2>${esc(group.title)}</h2></div>${group.keys.map((key) => settingField(key, state.schema[key])).join("")}</section>`).join("");
    document.querySelectorAll("[data-config]").forEach((input) => { const value = state.values[input.dataset.config]; if (input.type === "checkbox") input.checked = value !== false && Boolean(value); else input.value = value ?? ""; });
  }
  async function loadAll() {
    try {
      const [dashboard, settings] = await Promise.all([get("/dashboard"), get("/settings")]);
      showOverview(dashboard); fillSettings(settings);
      state.sessions = structuredClone(settings.sessions || []); state.groups = dashboard.groups || [];
      state.ready = true; dirty(false); renderSessions(); renderBindings();
    } catch (error) {
      notice(`加载失败：${error.message}`, true);
      if (!state.ready) { $("session-cards").innerHTML = '<p class="empty-state">无法读取会话数据，请从 AstrBot 插件页面打开或刷新。</p>'; document.querySelector(".status-pill").textContent = "连接失败"; }
    }
  }
  async function saveAll(showSuccess = true) {
    if (!state.ready || state.busy) { notice("配置尚未加载或正在保存，请稍后重试", true); return false; }
    state.busy = true;
    document.querySelectorAll("[data-save]").forEach((button) => { button.disabled = true; });
    const values = { ...state.values };
    document.querySelectorAll("[data-config]").forEach((input) => { values[input.dataset.config] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; });
    if ($("api-key").value.trim()) values.api_key = $("api-key").value.trim();
    document.querySelectorAll("[data-secret]").forEach((input) => { if (input.value.trim()) values[input.dataset.secret] = input.value.trim(); });
    const sessions = state.sessions.map((s) => ({ __template_key: "session", umo: s.umo, label: s.label, enabled: s.enabled, season_keyword_enabled: s.season_keyword_enabled !== false, output_mode: s.output_mode || "inherit", subscriptions: s.subscriptions || [] }));
    try { await post("/settings", { values, sessions }); await loadAll(); if (showSuccess) notice("订阅和配置已保存"); return true; }
    catch (error) { notice(`保存失败：${error.message}`, true); return false; }
    finally { state.busy = false; document.querySelectorAll("[data-save]").forEach((button) => { button.disabled = false; }); }
  }
  async function loadArchive() {
    const requestId = ++state.logRequest;
    $("log-summary").textContent = "正在读取…";
    try {
      const result = await get("/push-archive"); if (requestId !== state.logRequest) return;
      const previous = $("log-date").value; const days = result.days || [];
      $("log-date").innerHTML = days.map((day) => `<option value="${esc(day.date)}">${esc(day.date)} · ${day.entry_count} 条</option>`).join("");
      if (days.some((day) => day.date === previous)) $("log-date").value = previous;
      if (!days.length) { state.entries = []; renderLogs(); return; }
      await loadDay();
    } catch (error) { $("log-summary").textContent = `加载失败：${error.message}`; $("log-entries").replaceChildren(); }
  }
  async function loadDay() {
    const requestId = ++state.logRequest;
    const day = $("log-date").value;
    if (!day) return;
    try {
      const result = await get(`/push-archive/${encodeURIComponent(day)}`); if (requestId !== state.logRequest) return;
      state.entries = result.entries || []; renderLogs();
    } catch (error) { $("log-summary").textContent = `读取失败：${error.message}`; $("log-entries").replaceChildren(); }
  }
  function renderLogs() {
    const umos = [...new Set([...state.sessions.map((s) => s.umo), ...state.entries.map((e) => e.umo), ...(state.logUmo ? [state.logUmo] : [])])].filter(Boolean);
    $("log-session").innerHTML = '<option value="">全部会话</option>' + umos.map((umo) => `<option value="${esc(umo)}">${esc(state.sessions.find((s) => s.umo === umo)?.label || umo)}</option>`).join("");
    $("log-session").value = state.logUmo;
    const status = $("log-status").value;
    const entries = state.entries.filter((e) => (!state.logUmo || e.umo === state.logUmo) && (!status || status === e.status));
    $("log-summary").textContent = `${entries.length} 条 · ${entries.filter((e) => e.status === "sent").length} 成功 · ${entries.filter((e) => e.status === "failed" || e.status === "fetch_failed").length} 失败`;
    $("log-entries").innerHTML = entries.length ? entries.map((entry, index) => `<article class="panel log-card"><div class="panel-heading"><span class="eyebrow">${esc(time(entry.created_at))}</span><span class="tag ${entry.status === "sent" ? "live" : entry.status === "recorded" ? "paused-tag" : "error-tag"}">${esc(labels[entry.status] || entry.status)}</span></div><h3>${esc(entry.player)}</h3><code>${esc(entry.umo)}</code>${entry.score_delta == null ? "" : `<div class="score-journey"><span>${entry.from_score} <small>RP</small></span><span>→</span><strong>${entry.to_score} <small>RP</small></strong><b class="${entry.score_delta > 0 ? "positive" : "negative"}">${entry.score_delta > 0 ? "+" : ""}${entry.score_delta}</b></div>`}<p class="field-hint">${esc(entry.platform)} · ${esc(entry.rank || "")} · ${entry.mode === "record" ? "仅记录" : "通报+记录"}</p>${entry.error ? `<p class="session-warning">${esc(entry.error)}</p>` : ""}<details><summary>查看消息内容</summary><pre>${esc(entry.message)}</pre></details>${entry.image ? `<button class="text-button preview-button" data-preview="${index}">预览归档卡片 ↗</button>` : ""}</article>`).join("") : '<div class="panel empty-state">所选日期和会话暂无记录。启用归档后，分数变化和取数失败会自动记录。</div>';
    document.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", async () => {
      const entry = entries[Number(button.dataset.preview)]; button.disabled = true;
      try { const result = await get(`/push-archive/${encodeURIComponent(entry.date)}/${encodeURIComponent(entry.id)}/preview`); $("preview-image").src = `data:image/png;base64,${result.image}`; $("card-preview").showModal(); }
      catch (error) { notice(`预览失败：${error.message}`, true); }
      finally { button.disabled = false; }
    }));
  }
  function renderBindings() {
    $("binding-groups").innerHTML = state.groups.length ? state.groups.map((group, gi) => `<section class="panel"><div class="panel-heading"><h3>群 ${esc(group.group_id)}</h3><span class="tag">${group.bindings.length} 个绑定</span></div>${group.bindings.length ? group.bindings.map((binding, bi) => `<div class="binding-display"><strong>${esc(binding.user_id)}</strong><span>${esc(binding.target)}</span><button class="text-button" data-binding="${gi}:${bi}">解除</button></div>`).join("") : '<p class="field-hint">暂无个人查询绑定</p>'}</section>`).join("") : '<p class="empty-state">暂无群内绑定，可以通过上方表单添加。</p>';
    document.querySelectorAll("[data-binding]").forEach((button) => button.addEventListener("click", async () => {
      const [gi, bi] = button.dataset.binding.split(":").map(Number); const group = state.groups[gi]; const binding = group.bindings[bi];
      try { await post("/bindings", { group_id: group.group_id, user_id: binding.user_id, target: "" }); state.groups = (await get("/bindings")).groups || []; renderBindings(); notice("已解除个人查询绑定"); }
      catch (error) { notice(error.message, true); }
    }));
  }
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => openPage(item.dataset.page)));
  document.querySelectorAll("[data-page-target]").forEach((item) => item.addEventListener("click", () => openPage(item.dataset.pageTarget)));
  document.querySelectorAll("[data-save]").forEach((item) => item.addEventListener("click", () => void saveAll()));
  document.addEventListener("input", (event) => { if (event.target.matches("[data-config], [data-secret]")) dirty(); });
  document.addEventListener("change", (event) => { if (event.target.matches("[data-config], [data-secret]")) dirty(); });
  $("refresh").addEventListener("click", () => { if (state.dirty) { notice("请先保存当前更改，再刷新数据", true); return; } void loadAll(); });
  $("add-session").addEventListener("click", () => { if (!state.ready) return; state.sessions.push({ _new: true, label: "", umo: "", group_id: "", enabled: true, season_keyword_enabled: true, allowed: true, subscriptions: [], available_players: [], output_mode: "inherit" }); dirty(); renderSessions(); document.querySelector(".session-card:last-child [data-umo]").focus(); });
  $("session-search").addEventListener("input", filterSessions);
  document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => { state.filter = button.dataset.filter; document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("active", item === button)); filterSessions(); }));
  $("refresh-logs").addEventListener("click", () => void loadArchive());
  $("log-date").addEventListener("change", () => void loadDay());
  $("log-session").addEventListener("change", (event) => { state.logUmo = event.target.value; renderLogs(); });
  $("log-status").addEventListener("change", renderLogs);
  $("binding-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.currentTarget.querySelector("button"); button.disabled = true;
    try { await post("/bindings", { group_id: $("binding-group").value.trim(), user_id: $("binding-user").value.trim(), target: $("binding-target").value.trim() }); state.groups = (await get("/bindings")).groups || []; renderBindings(); notice("个人查询绑定已保存；需要自动推送请添加会话订阅"); }
    catch (error) { notice(error.message, true); } finally { button.disabled = false; }
  });
  $("close-preview").addEventListener("click", () => $("card-preview").close());
  window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
  if (bridge?.ready) bridge.ready().then(loadAll).catch((error) => notice(error.message, true)); else void loadAll();
})();
