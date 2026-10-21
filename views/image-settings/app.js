const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const profilesEl = $("profiles");
const template = $("profile-template");
const savedUrls = $("saved-urls");
const state = { profiles: [], active: 0, autoFallback: true, backend: "" };
let busy = false;
let testPolling = false;

/* ---------- 通用 ---------- */

function show(message, tone = "") {
  const box = $("result");
  box.hidden = false;
  box.textContent = message;
  box.dataset.tone = tone;
}

function setCardStatus(card, message, tone = "") {
  const element = card.querySelector(".profile-status");
  element.textContent = message;
  element.dataset.tone = tone;
}

function parseKeys(text) {
  const keys = text
    .split(/\r?\n/)
    .map((key) => key.trim())
    .filter(Boolean);
  return keys.length ? keys : null;
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function newId() {
  const random =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `new-${random}`;
}

/* ---------- 渲染 ---------- */

function render() {
  profilesEl.replaceChildren();
  state.profiles.forEach((profile, index) => {
    profilesEl.append(buildCard(profile, index));
  });
  $("auto-fallback").checked = state.autoFallback;
  $("empty").hidden = state.profiles.length > 0;
  updateSavedUrls();
}

function buildCard(profile, index) {
  const card = template.content.firstElementChild.cloneNode(true);

  const name = card.querySelector(".name");
  name.value = profile.name || "";
  name.addEventListener("input", () => {
    profile.name = name.value;
  });

  const active = card.querySelector(".active");
  active.checked = index === state.active;
  active.addEventListener("change", () => {
    if (active.checked) state.active = index;
  });

  const baseUrl = card.querySelector(".base-url");
  baseUrl.value = profile.base_url || "";
  baseUrl.addEventListener("input", () => {
    profile.base_url = baseUrl.value;
    updateSavedUrls();
  });

  const keys = card.querySelector(".keys");
  keys.value =
    profile.api_key && profile.api_key.length ? profile.api_key.join("\n") : "";
  keys.placeholder = profile.key_count
    ? `已配置 ${profile.key_count} 条；留空保留，填写会替换`
    : "粘贴密钥，每行一条";
  keys.addEventListener("input", () => {
    profile.api_key = parseKeys(keys.value);
  });
  const keyHint = card.querySelector(".key-hint");
  keyHint.textContent = profile.key_count
    ? `已配置 ${profile.key_count} 条密钥`
    : "还没有密钥";

  const select = card.querySelector(".model");
  const custom = card.querySelector(".model-custom");
  select.addEventListener("change", () => {
    if (select.value === "__manual__") {
      custom.hidden = false;
      custom.focus();
      profile.model = custom.value.trim();
    } else {
      custom.hidden = true;
      profile.model = select.value;
    }
    updateModelHint(card, profile);
  });
  custom.addEventListener("input", () => {
    if (select.value === "__manual__") {
      profile.model = custom.value.trim();
    }
  });
  renderModel(card, profile);

  const up = card.querySelector('[data-action="up"]');
  const down = card.querySelector('[data-action="down"]');
  up.disabled = index === 0;
  down.disabled = index === state.profiles.length - 1;

  card.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    event.preventDefault();
    handleAction(button.dataset.action, index, card);
  });

  return card;
}

function renderModel(card, profile) {
  const select = card.querySelector(".model");
  const custom = card.querySelector(".model-custom");
  const models = unique(profile.models || []);
  const current = (profile.model || "").trim();
  const options =
    current && !models.includes(current) ? [...models, current] : models.slice();
  select.replaceChildren(
    ...options.map((id) => new Option(id, id)),
    new Option("手动填写模型 ID", "__manual__"),
  );
  if (current) {
    select.value = current;
    custom.hidden = true;
    custom.value = "";
  } else {
    select.value = "__manual__";
    custom.hidden = false;
    custom.value = "";
  }
  updateModelHint(card, profile);
}

function updateModelHint(card, profile) {
  const hint = card.querySelector(".model-hint");
  const models = unique(profile.models || []);
  const current = (profile.model || "").trim();
  if (!models.length) {
    hint.textContent = "可刷新模型或手动填写";
    return;
  }
  let text = `已检测到 ${models.length} 个模型`;
  if (current && !models.includes(current)) {
    text += "；当前模型不在检测结果中";
  }
  hint.textContent = text;
}

function updateSavedUrls() {
  const urls = unique(
    state.profiles.map((profile) => (profile.base_url || "").trim()),
  );
  savedUrls.replaceChildren(...urls.map((url) => new Option(url, url)));
}

/* ---------- 操作 ---------- */

function syncCard(profile, card) {
  const select = card.querySelector(".model");
  const custom = card.querySelector(".model-custom");
  profile.name = card.querySelector(".name").value.trim();
  profile.base_url = card.querySelector(".base-url").value.trim();
  profile.api_key = parseKeys(card.querySelector(".keys").value);
  profile.model =
    select.value === "__manual__" ? custom.value.trim() : select.value;
}

function profilePayload(profile) {
  const data = {
    id: profile.id,
    name: profile.name,
    base_url: profile.base_url,
    model: profile.model,
    timeout: profile.timeout || 300,
  };
  if (profile.api_key && profile.api_key.length) {
    data.api_key = profile.api_key;
  }
  return data;
}

function missingCredentials(profile, card) {
  if (!profile.base_url) {
    setCardStatus(card, "请先填写接口地址。", "error");
    return true;
  }
  if (!profile.key_count && !(profile.api_key && profile.api_key.length)) {
    setCardStatus(card, "请先填写密钥（新方案必须填写）。", "error");
    return true;
  }
  return false;
}

function handleAction(action, index, card) {
  const profile = state.profiles[index];
  if (!profile) return;
  syncCard(profile, card);
  if (action === "up" || action === "down") {
    moveProfile(index, action === "up" ? -1 : 1);
    return;
  }
  if (action === "delete") {
    deleteProfile(index);
    return;
  }
  if (action === "models") {
    refreshModels(index, card);
    return;
  }
  if (action === "connect") {
    testConnectivity(index, card);
    return;
  }
  if (action === "test") {
    testImage(index, card);
  }
}

function moveProfile(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.profiles.length) return;
  const activeProfile = state.profiles[state.active];
  const current = state.profiles[index];
  state.profiles[index] = state.profiles[target];
  state.profiles[target] = current;
  state.active = Math.max(0, state.profiles.indexOf(activeProfile));
  render();
}

function deleteProfile(index) {
  if (state.profiles.length <= 1) {
    show("至少保留一个接口方案。", "error");
    return;
  }
  const activeProfile = state.profiles[state.active];
  const removed = state.profiles[index];
  state.profiles.splice(index, 1);
  if (removed === activeProfile) {
    state.active = Math.min(index, state.profiles.length - 1);
  } else {
    state.active = Math.max(0, state.profiles.indexOf(activeProfile));
  }
  render();
}

function addProfile() {
  state.profiles.push({
    id: newId(),
    name: "",
    base_url: "",
    model: "",
    timeout: 300,
    key_count: 0,
    api_key: null,
    models: [],
  });
  if (state.profiles.length === 1) state.active = 0;
  render();
  profilesEl.lastElementChild?.querySelector(".name")?.focus();
}

async function refreshModels(index, card) {
  const profile = state.profiles[index];
  syncCard(profile, card);
  if (missingCredentials(profile, card)) return;
  const button = card.querySelector('[data-action="models"]');
  button.disabled = true;
  setCardStatus(card, "正在获取模型列表…");
  try {
    const data = await bridge.apiPost("models", profilePayload(profile));
    profile.models = unique(data.models || []);
    renderModel(card, profile);
    setCardStatus(
      card,
      profile.models.length
        ? `已获取 ${profile.models.length} 个模型，请从下拉中选择。`
        : "服务端没有返回模型，可手动填写模型 ID。",
      profile.models.length ? "success" : "",
    );
  } catch (error) {
    setCardStatus(
      card,
      error.message || "自动获取模型失败，仍可手动填写模型 ID。",
      "error",
    );
  } finally {
    button.disabled = false;
  }
}

async function testConnectivity(index, card) {
  const profile = state.profiles[index];
  syncCard(profile, card);
  if (missingCredentials(profile, card)) return;
  const button = card.querySelector('[data-action="connect"]');
  button.disabled = true;
  setCardStatus(card, "正在检查接口连接和鉴权…");
  try {
    const data = await bridge.apiPost("connectivity", profilePayload(profile));
    profile.models = unique(data.models || []);
    renderModel(card, profile);
    const listed = data.model_listed
      ? "所选模型已在列表中。"
      : "所选模型未在列表中，可检查模型名或做实际生图测试。";
    setCardStatus(
      card,
      `接口连通，鉴权通过 · ${data.elapsed_ms} ms\n${listed}\n尚未验证图片编辑能力，本次未发起生图。`,
      "success",
    );
  } catch (error) {
    setCardStatus(card, error.message || "连通性测试失败。", "error");
  } finally {
    button.disabled = false;
  }
}

async function waitTestStatus(onProgress) {
  let status = await bridge.apiGet("image-test");
  while (status.status === "running") {
    if (onProgress) onProgress(status);
    await new Promise((resolve) => setTimeout(resolve, 1500));
    status = await bridge.apiGet("image-test");
  }
  return status;
}

async function testImage(index, card) {
  if (testPolling) {
    setCardStatus(card, "已有测试进行中，请稍候。", "error");
    return;
  }
  const profile = state.profiles[index];
  syncCard(profile, card);
  if (missingCredentials(profile, card)) return;
  const button = card.querySelector('[data-action="test"]');
  button.disabled = true;
  testPolling = true;
  $("preview").hidden = true;
  setCardStatus(card, "正在启动实际生图测试…");
  try {
    const started = await bridge.apiPost("image-test", profilePayload(profile));
    let status = started;
    if (started.status === "running") {
      status = await waitTestStatus((current) => {
        setCardStatus(
          card,
          `正在验证图片编辑接口 · ${current.model}\n生成可能需要几分钟，请勿重复提交。`,
        );
      });
    }
    showTest(status);
    setCardStatus(
      card,
      status.status === "success"
        ? "实际生图测试通过。"
        : status.status === "cancelled"
          ? "测试已取消。"
          : "实际生图测试失败。",
      status.status === "success" ? "success" : "error",
    );
  } catch (error) {
    setCardStatus(card, error.message || "实际生图测试失败。", "error");
  } finally {
    testPolling = false;
    button.disabled = false;
  }
}

function showTest(status) {
  if (status.status === "success") {
    show(
      `实际生图测试通过 · ${status.model}\n耗时 ${(status.elapsed_ms / 1000).toFixed(1)} 秒，已返回有效图片。`,
      "success",
    );
    $("test-preview").src = status.image;
    $("preview-caption").textContent = `模型：${status.model} · 本次测试结果`;
    $("preview").hidden = false;
  } else if (status.status === "error") {
    show(`实际生图测试失败 · ${status.model}\n${status.error}`, "error");
  } else if (status.status === "cancelled") {
    show("实际生图测试已取消，可重新测试。");
  }
}

/* ---------- 保存 / 加载 ---------- */

function applySettings(data, { keepModels = true } = {}) {
  const previous = new Map();
  if (keepModels) {
    for (const profile of state.profiles) {
      previous.set(profile.id, profile.models || []);
      if (profile.base_url) {
        previous.set(`url:${profile.base_url}`, profile.models || []);
      }
    }
  }
  state.profiles = (data.profiles || []).map((profile) => ({
    ...profile,
    api_key: null,
    models:
      previous.get(profile.id) ||
      previous.get(`url:${profile.base_url}`) ||
      [],
  }));
  state.active = Math.max(
    0,
    Math.min(Number(data.active) || 0, Math.max(0, state.profiles.length - 1)),
  );
  state.autoFallback = data.auto_fallback !== false;
  state.backend = data.backend || "";
  $("backend-note").textContent = state.backend
    ? `当前生图后端：${state.backend}（在插件配置里切换）`
    : "";
  render();
}

function setSaveBusy(value) {
  for (const id of ["save", "save-bottom"]) {
    const button = $(id);
    button.disabled = value;
    button.textContent = value ? "保存中…" : "保存设置";
  }
}

async function save() {
  if (busy) return;
  const cards = [...profilesEl.children];
  state.profiles.forEach((profile, index) => {
    if (cards[index]) syncCard(profile, cards[index]);
  });
  if (!state.profiles.length) {
    show("至少保留一个接口方案。", "error");
    return;
  }
  for (const profile of state.profiles) {
    const label = profile.name || "未命名方案";
    if (!profile.base_url) {
      show(`方案「${label}」缺少接口地址。`, "error");
      return;
    }
    if (!profile.model) {
      show(`方案「${label}」缺少模型名。`, "error");
      return;
    }
    if (!profile.key_count && !(profile.api_key && profile.api_key.length)) {
      show(`方案「${label}」缺少密钥。`, "error");
      return;
    }
  }
  busy = true;
  setSaveBusy(true);
  try {
    const data = await bridge.apiPost("settings", {
      profiles: state.profiles.map(profilePayload),
      active: state.active,
      auto_fallback: state.autoFallback,
    });
    applySettings(data);
    show("设置已保存，后续生成立即使用新配置。", "success");
  } catch (error) {
    show(error.message || "保存失败，请稍后再试。", "error");
  } finally {
    busy = false;
    setSaveBusy(false);
  }
}

async function init() {
  if (!bridge) {
    show("请从 AstrBot 的插件页面打开「生图设置」。", "error");
    return;
  }
  try {
    await bridge.ready();
    applySettings(await bridge.apiGet("settings"));
    const test = await bridge.apiGet("image-test");
    if (test.status === "running") {
      show(`正在验证图片编辑接口 · ${test.model}`, "info");
      const status = await waitTestStatus((current) => {
        show(`正在验证图片编辑接口 · ${current.model}`, "info");
      });
      showTest(status);
    } else if (test.status !== "idle") {
      showTest(test);
    }
  } catch (error) {
    show(error.message || "设置加载失败，请刷新页面。", "error");
  }
}

$("add").addEventListener("click", addProfile);
$("add-bottom").addEventListener("click", addProfile);
$("save").addEventListener("click", save);
$("save-bottom").addEventListener("click", save);
$("auto-fallback").addEventListener("change", (event) => {
  state.autoFallback = event.target.checked;
});

init();
