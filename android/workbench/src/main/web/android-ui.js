(() => {
  "use strict";

  const api = window.pywebview.api;
  const html = document.documentElement;
  const layout = document.getElementById("layout");
  const sidebar = document.getElementById("sidebar");
  const conversation = layout.querySelector(".conversation");
  const frame = document.querySelector(".window");

  const tabs = document.createElement("nav");
  tabs.className = "mobile-tabs";
  tabs.setAttribute("aria-label", "工作台导航");
  tabs.innerHTML =
    '<button type="button" data-tab="chat"><svg class="icon" aria-hidden="true"><use href="#i-chat"/></svg>会话</button>' +
    '<button type="button" data-tab="people"><svg class="icon" aria-hidden="true"><use href="#i-person"/></svg>人物</button>' +
    '<button type="button" data-tab="settings"><svg class="icon" aria-hidden="true"><use href="#i-more"/></svg>设置</button>';
  frame.insertBefore(tabs, document.getElementById("toast"));

  const settings = document.createElement("section");
  settings.className = "mobile-settings";
  settings.setAttribute("aria-labelledby", "mobile-settings-title");
  settings.innerHTML =
    '<h1 id="mobile-settings-title">设置</h1>' +
    '<div class="mobile-card"><h2>DeepSeek 官方连接</h2>' +
    '<p id="mobile-key-state" role="status">正在读取保存状态。</p>' +
    '<button type="button" class="button primary" id="mobile-open-key">设置 API Key</button></div>' +
    '<div class="mobile-card" id="mobile-appearance"></div>' +
    '<div class="mobile-card"><button type="button" class="button" id="mobile-jev">决策模型设置</button></div>' +
    '<div class="mobile-card"><button type="button" class="button" id="mobile-diagnostics">运行与诊断</button></div>';
  layout.append(settings);
  const theme = document.getElementById("mobile-appearance");
  for (const control of document.querySelectorAll("#app-menu .appearance-setting")) theme.append(control);

  const connection = document.getElementById("mobile-key-state");
  const keyButton = document.getElementById("mobile-open-key");
  document.getElementById("mobile-diagnostics").addEventListener("click", () => window.pcarLive.diagnostics());
  document.getElementById("mobile-jev").addEventListener("click", () => window.pcarLive.decisionSettings());
  let revision = 0;
  async function readSettingsStatus() {
    const request = ++revision;
    try {
      const result = await api.live_health();
      if (request !== revision) return;
      connection.textContent = result.state === "online"
        ? (result.configured ? "API Key 已保存" : "尚未保存 API Key")
        : "正在连接本机服务。";
    } catch {
      if (request === revision) connection.textContent = "暂时无法读取本机配置。";
    }
  }

  function sync() {
    const tab = html.dataset.mobileTab;
    sidebar.inert = tab !== "people";
    conversation.inert = tab !== "chat";
    settings.inert = tab !== "settings";
    layout.classList.remove("drawer-open", "collapsed");
    for (const button of tabs.querySelectorAll("button")) {
      if (button.dataset.tab === tab) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
  }

  function show(tab) {
    html.dataset.mobileTab = tab;
    sync();
    if (tab === "settings") readSettingsStatus();
    if (tab === "chat") window.pcarLive?.wake();
  }

  tabs.addEventListener("click", event => {
    const tab = event.target.closest("button[data-tab]");
    if (tab) show(tab.dataset.tab);
  });
  sidebar.addEventListener("click", event => {
    if (event.target.closest("#people button, #sessions button, #new-session")) show("chat");
  });
  keyButton.addEventListener("click", async () => {
    keyButton.disabled = true;
    try {
      if (await api.open_key_settings()) window.pcarLive.wake();
    } catch (error) {
      notify(error.message);
    } finally {
      keyButton.disabled = false;
      readSettingsStatus();
    }
  });

  async function recoverDocument() {
    const pending = await api.recover_document().catch(() => null);
    if (pending) window.pcarLive.recoverDocument(pending);
  }

  window.pcarMobile = Object.freeze({
    sync,
    chat() { show("chat"); },
    recoverDocument,
    back() {
      if (window.pcarAnnouncements?.close()) return true;
      const modal = document.getElementById("dialog");
      if (modal.open) {
        modal.close();
        return true;
      }
      if (html.dataset.mobileTab !== "chat") {
        show("chat");
        return true;
      }
      return false;
    },
    pause() { window.pcarLive.stop(); },
    resume() {
      window.pcarLive.resume();
      window.pcarLive.wake();
      recoverDocument();
      if (html.dataset.mobileTab === "settings") readSettingsStatus();
    }
  });

  show("chat");
  recoverDocument();
  api.appearance().then(appearance => {
    html.dataset.appearance = appearance.mode;
    appearanceMode.value = appearance.mode;
    savedAccent = appearance.accent;
    showAccent(appearance.accent);
  }).catch(() => {});
})();
