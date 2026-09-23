const bridge = window.AstrBotPluginPage;

const COVER_LABELS = {
  image: "直接发送图片",
  link: "只发图片链接",
  off: "不附带封面",
};

const elements = {
  runtimeStatus: document.getElementById("runtime-status"),
  refreshButton: document.getElementById("refresh-button"),
  cycleButton: document.getElementById("cycle-button"),
  errorNotice: document.getElementById("error-notice"),
  sessionList: document.getElementById("session-list"),
  sessionSearch: document.getElementById("session-search"),
  detailView: document.getElementById("detail-view"),
  sourceBand: document.getElementById("source-band"),
  toast: document.getElementById("toast"),
  metrics: {
    sessions: document.getElementById("metric-sessions"),
    enabled: document.getElementById("metric-enabled"),
    games: document.getElementById("metric-games"),
    reminders: document.getElementById("metric-reminders"),
  },
};

const state = {
  overview: null,
  selectedUmo: null,
  search: "",
  loading: true,
  saving: false,
  toastTimer: null,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.classList.remove("is-hidden");
  state.toastTimer = window.setTimeout(() => {
    elements.toast.classList.add("is-hidden");
  }, 3200);
}

function setError(message = "") {
  elements.errorNotice.textContent = message;
  elements.errorNotice.classList.toggle("is-hidden", !message);
}

function setRuntime(text, kind = "") {
  elements.runtimeStatus.querySelector("span:last-child").textContent = text;
  elements.runtimeStatus.classList.toggle("is-ready", kind === "ready");
  elements.runtimeStatus.classList.toggle("is-error", kind === "error");
}

function setSaving(saving) {
  state.saving = saving;
  document.querySelectorAll("button, input, textarea").forEach((control) => {
    control.disabled = saving;
  });
}

function currentSession() {
  if (!state.overview) return null;
  const sessions = state.overview.sessions || [];
  return sessions.find((item) => item.umo === state.selectedUmo) || null;
}

const MISSING_BRIDGE_HINT =
  "未能连接到 AstrBot Dashboard：桥接脚本（AstrBotPluginPage）未加载。\n" +
  "请通过 AstrBot 后台 → 插件 → 本插件详情页打开此页面；" +
  "若直接访问该 URL 或插件未重载，就会出现此提示。";

function bridgeMissing() {
  setError(MISSING_BRIDGE_HINT);
  setRuntime("未连接 Dashboard", "error");
}

async function loadOverview({ announce = false } = {}) {
  if (!bridge) {
    bridgeMissing();
    return;
  }
  state.loading = true;
  elements.refreshButton.classList.add("is-loading");
  try {
    state.overview = await bridge.apiGet("overview");
    const sessions = state.overview.sessions || [];
    if (!sessions.some((item) => item.umo === state.selectedUmo)) {
      state.selectedUmo = sessions.length ? sessions[0].umo : null;
    }
    render();
    setRuntime("已连接", "ready");
    setError("");
    if (announce) showToast("数据已刷新");
  } catch (error) {
    setError(error?.message || "读取订阅数据失败");
    setRuntime("读取失败", "error");
    if (announce) showToast("刷新失败", true);
  } finally {
    state.loading = false;
    elements.refreshButton.classList.remove("is-loading");
  }
}

async function post(endpoint, payload, successMessage) {
  setSaving(true);
  try {
    await bridge.apiPost(endpoint, payload);
    await loadOverview();
    if (successMessage) showToast(successMessage);
    return true;
  } catch (error) {
    showToast(error?.message || "保存失败", true);
    return false;
  } finally {
    setSaving(false);
  }
}

function render() {
  renderMetrics();
  renderSources();
  renderSessionList();
  renderDetail();
}

function renderMetrics() {
  const overview = state.overview;
  if (!overview) return;
  const sessions = overview.sessions || [];
  elements.metrics.sessions.textContent = String(sessions.length);
  elements.metrics.enabled.textContent = String(
    sessions.filter((item) => item.enabled).length,
  );
  elements.metrics.games.textContent = String((overview.games || []).length);
  const cycle = overview.cycle || {};
  const reminders = Number(cycle.reminders || 0);
  elements.metrics.reminders.textContent = cycle.finished_at
    ? String(reminders)
    : "待运行";
}

function renderSources() {
  const overview = state.overview;
  if (!overview) return;
  const parts = (overview.sources || []).map((source) => {
    const title = source.enabled ? source.note : "已在配置中关闭";
    return `<span class="source-chip ${source.enabled ? "is-on" : ""}" title="${escapeHtml(title)}">
      <span class="dot"></span>${escapeHtml(source.name)}
    </span>`;
  });
  const cycle = overview.cycle || {};
  const failures = Object.entries(cycle.failures || {});
  if (cycle.finished_at) {
    parts.push(
      `<span class="source-chip" title="上一轮检查时间">
        上轮检查 ${escapeHtml(cycle.finished_at)}
      </span>`,
    );
  }
  if (failures.length) {
    const detail = failures.map(([id, text]) => `${id}: ${text}`).join("\n");
    parts.push(
      `<span class="source-chip" title="${escapeHtml(detail)}" style="color:var(--danger)">
        ⚠️ ${failures.length} 个数据源异常
      </span>`,
    );
  }
  elements.sourceBand.innerHTML = parts.join("");
}

function filteredSessions() {
  const sessions = state.overview?.sessions || [];
  const keyword = state.search.trim().toLowerCase();
  if (!keyword) return sessions;
  return sessions.filter((item) =>
    `${item.session_name} ${item.session_id} ${item.umo} ${item.game_names.join(" ")}`
      .toLowerCase()
      .includes(keyword),
  );
}

function renderSessionList() {
  const sessions = filteredSessions();
  if (!sessions.length) {
    elements.sessionList.innerHTML =
      '<li class="empty-state">没有匹配的会话</li>';
    return;
  }
  elements.sessionList.innerHTML = sessions
    .map((item) => {
      const active = item.umo === state.selectedUmo;
      const statusBadge = item.enabled
        ? '<span class="badge is-on">推送中</span>'
        : '<span class="badge is-off">已暂停</span>';
      const games = item.game_names.join("、") || "未选择游戏";
      return `<li class="session-item ${active ? "is-active" : ""}" data-umo="${escapeHtml(item.umo)}">
        <div class="session-name">${escapeHtml(item.session_name)} ${statusBadge}</div>
        <div class="session-meta">${escapeHtml(games)}</div>
      </li>`;
    })
    .join("");

  elements.sessionList.querySelectorAll(".session-item").forEach((node) => {
    node.addEventListener("click", () => {
      state.selectedUmo = node.dataset.umo;
      render();
    });
  });
}

function renderDetail() {
  const session = currentSession();
  if (!session) {
    elements.detailView.innerHTML =
      '<div class="empty-state">左侧选择一个会话来管理它的活动订阅。<br />在群聊中使用 <code>/活动订阅 &lt;游戏名&gt;</code> 即可创建订阅。</div>';
    return;
  }

  const games = state.overview.games || [];
  const defaults = state.overview.defaults || {};
  const selected = new Set(session.games || []);
  const thresholdsValue = (session.thresholds || []).join(", ");

  const gameOptions = games
    .map(
      (game) => `<label class="game-option">
        <input type="checkbox" name="games" value="${escapeHtml(game.id)}"
          ${selected.has(game.id) ? "checked" : ""} />
        <span>${escapeHtml(game.name)}
          <span class="game-source">${escapeHtml(game.source_name)}</span>
        </span>
      </label>`,
    )
    .join("");

  elements.detailView.innerHTML = `
    <h2>${escapeHtml(session.session_name)}</h2>
    <div class="detail-umo">${escapeHtml(session.umo)}</div>

    <form class="detail-form" id="detail-form">
      <div class="field-group">
        <div class="toggle-row">
          <label class="toggle">
            <input type="checkbox" id="field-enabled" ${session.enabled ? "checked" : ""} />
            <span>启用活动推送</span>
          </label>
          <label class="toggle">
            <input type="checkbox" id="field-notify-new" ${session.notify_new ? "checked" : ""} />
            <span>推送新活动</span>
          </label>
        </div>
      </div>

      <fieldset class="field-group">
        <legend>订阅游戏（${selected.size} / ${games.length}）</legend>
        <div class="game-grid">${gameOptions || "没有可用的游戏，请在插件配置中启用数据源"}</div>
      </fieldset>

      <fieldset class="field-group">
        <legend>封面形式</legend>
        <div class="field-row">
          <label class="toggle">
            <input type="checkbox" id="field-cover-default" ${session.cover_mode_overridden ? "" : "checked"} />
            <span>跟随全局默认（${escapeHtml(COVER_LABELS[defaults.cover_mode] || defaults.cover_mode || "直接发送图片")}）</span>
          </label>
          <label for="field-cover-mode">本会话封面形式</label>
          <select id="field-cover-mode" ${session.cover_mode_overridden ? "" : "disabled"}>
            <option value="image" ${session.effective_cover_mode === "image" ? "selected" : ""}>直接发送图片</option>
            <option value="link" ${session.effective_cover_mode === "link" ? "selected" : ""}>只发图片链接</option>
            <option value="off" ${session.effective_cover_mode === "off" ? "selected" : ""}>不附带封面</option>
          </select>
          <span class="field-hint">也可以在本会话直接用 <code>/活动封面 图片</code> 或 <code>/活动封面 链接</code> 切换。</span>
        </div>
      </fieldset>

      <fieldset class="field-group">
        <legend>倒计时提醒</legend>
        <div class="field-row">
          <label class="toggle">
            <input type="checkbox" id="field-use-default" ${session.thresholds_overridden ? "" : "checked"} />
            <span>使用全局默认档位（${escapeHtml((defaults.thresholds || []).join("、") || "已关闭")}）</span>
          </label>
          <label for="field-thresholds">自定义档位（天，逗号分隔；留空表示关闭）</label>
          <input type="text" id="field-thresholds" value="${escapeHtml(thresholdsValue)}"
            placeholder="例如 7, 3, 1" ${session.thresholds_overridden ? "" : "disabled"} />
          <span class="field-hint">1 表示剩余不足 24 小时。活动剩余时间进入某一档位时提醒一次。</span>
        </div>
      </fieldset>

      <fieldset class="field-group">
        <legend>关键词过滤</legend>
        <div class="field-row">
          <label for="field-blacklist">黑名单（命中任一关键词就不推送）</label>
          <textarea id="field-blacklist" rows="2" placeholder="例如 双倍掉落, 签到">${escapeHtml((session.blacklist || []).join(", "))}</textarea>
        </div>
        <div class="field-row" style="margin-top:10px">
          <label for="field-whitelist">白名单（填写后只推送命中的活动）</label>
          <textarea id="field-whitelist" rows="2" placeholder="例如 寻访, 主线">${escapeHtml((session.whitelist || []).join(", "))}</textarea>
        </div>
        <span class="field-hint">匹配对象为活动名称、描述与标签，逗号分隔，不区分大小写。</span>
      </fieldset>

      <div class="detail-actions">
        <button type="submit" class="primary-button">保存设置</button>
        <button type="button" id="preview-button">预览当前活动</button>
        <button type="button" id="remove-button" class="danger-button">移除订阅</button>
      </div>

      <pre class="preview-output is-hidden" id="preview-output"></pre>
    </form>
  `;

  const useDefault = document.getElementById("field-use-default");
  const thresholdsInput = document.getElementById("field-thresholds");
  useDefault.addEventListener("change", () => {
    thresholdsInput.disabled = useDefault.checked;
  });

  const coverDefault = document.getElementById("field-cover-default");
  const coverSelect = document.getElementById("field-cover-mode");
  coverDefault.addEventListener("change", () => {
    coverSelect.disabled = coverDefault.checked;
  });

  document
    .getElementById("detail-form")
    .addEventListener("submit", (event) => {
      event.preventDefault();
      submitDetail(session);
    });

  document
    .getElementById("preview-button")
    .addEventListener("click", () => previewGame(session));

  document
    .getElementById("remove-button")
    .addEventListener("click", () => {
      if (!window.confirm(`确定要移除「${session.session_name}」的全部活动订阅吗？`)) {
        return;
      }
      post("session/remove", { umo: session.umo }, "已移除该会话的订阅");
    });
}

function parseThresholds(text) {
  return String(text || "")
    .split(/[,，\s]+/)
    .map((item) => Number.parseInt(item, 10))
    .filter((value) => Number.isFinite(value) && value > 0);
}

function parseKeywords(text) {
  const seen = [];
  String(text || "")
    .split(/[,，\n]+/)
    .map((item) => item.trim())
    .forEach((item) => {
      if (item && !seen.includes(item)) seen.push(item);
    });
  return seen;
}

async function submitDetail(session) {
  const games = Array.from(
    document.querySelectorAll('input[name="games"]:checked'),
  ).map((node) => node.value);
  if (!games.length) {
    showToast("请至少保留一个订阅游戏", true);
    return;
  }

  const useDefault = document.getElementById("field-use-default").checked;
  const coverDefault = document.getElementById("field-cover-default").checked;
  const payload = {
    umo: session.umo,
    games,
    enabled: document.getElementById("field-enabled").checked,
    notify_new: document.getElementById("field-notify-new").checked,
    cover_mode: coverDefault
      ? null
      : document.getElementById("field-cover-mode").value,
    thresholds: useDefault
      ? null
      : parseThresholds(document.getElementById("field-thresholds").value),
    blacklist: parseKeywords(document.getElementById("field-blacklist").value),
    whitelist: parseKeywords(document.getElementById("field-whitelist").value),
  };
  await post("session/update", payload, "设置已保存");
}

async function previewGame(session) {
  const games = Array.from(
    document.querySelectorAll('input[name="games"]:checked'),
  ).map((node) => node.value);
  const output = document.getElementById("preview-output");
  const gameId = games[0];
  if (!gameId) {
    showToast("请先勾选至少一个游戏", true);
    return;
  }
  output.classList.remove("is-hidden");
  output.textContent = "正在获取活动数据…";
  setSaving(true);
  try {
    const result = await bridge.apiPost("preview", { game_id: gameId });
    output.textContent = result?.text || "没有获取到活动数据";
  } catch (error) {
    output.textContent = `获取失败：${error?.message || error}`;
  } finally {
    setSaving(false);
  }
}

async function runCycle() {
  setSaving(true);
  try {
    const result = await bridge.apiPost("refresh", {});
    const summary = `检查完成：数据源 ${result.fetched_games} 个 / 活动 ${result.activities} 条 / 提醒 ${result.reminders} 条`;
    showToast(summary);
    if (result.failures && Object.keys(result.failures).length) {
      showToast("部分数据源异常，详见状态栏", true);
    }
    await loadOverview();
  } catch (error) {
    showToast(error?.message || "检查失败", true);
  } finally {
    setSaving(false);
  }
}

elements.refreshButton.addEventListener("click", () =>
  loadOverview({ announce: true }),
);
elements.cycleButton.addEventListener("click", runCycle);

elements.sessionSearch.addEventListener("input", (event) => {
  state.search = event.target.value;
  renderSessionList();
});

async function start() {
  if (!bridge) {
    bridgeMissing();
    return;
  }
  try {
    const context = await bridge.ready();
    document.title =
      bridge.t?.("pages.activity.title", "游戏活动订阅管理") ||
      "游戏活动订阅管理";
    document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
    bridge.onContext?.((nextContext) => {
      const current = nextContext || bridge.getContext?.();
      document.documentElement.dataset.theme = current?.isDark
        ? "dark"
        : "light";
    });
  } catch (error) {
    /* 上下文获取失败不影响主要功能 */
  }
  await loadOverview();
}

start().catch((error) => {
  setError(error?.message || "页面初始化失败");
});
