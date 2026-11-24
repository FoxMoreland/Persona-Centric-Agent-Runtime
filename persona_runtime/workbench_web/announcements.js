(() => {
  "use strict";

  const button = document.getElementById("announcement-open");
  const dialog = document.getElementById("announcement-dialog");
  const list = document.getElementById("announcement-list");
  const title = document.getElementById("announcement-title");
  const filters = document.getElementById("announcement-filters");
  const filter = document.getElementById("announcement-filter");
  const refresh = document.getElementById("announcement-refresh");
  const status = document.getElementById("announcement-status");
  const center = document.getElementById("announcement-center");
  const acknowledge = document.getElementById("announcement-ack");
  let data, active, connected = false, finishing = false;
  const api = () => window.pywebview?.api;

  function render(rows) {
    list.replaceChildren();
    for (const row of rows) {
      const article = document.createElement("article");
      article.className = "announcement-entry";
      const heading = document.createElement("h3");
      heading.textContent = row.title;
      const date = document.createElement("p");
      date.className = "hint";
      const platforms = row.platforms.map(value => value === "windows" ? "Windows" : "Android").join("、");
      date.textContent = [row.date, platforms, row.updated_at && "更新于 " + row.updated_at].filter(Boolean).join(" · ");
      const points = document.createElement("ul");
      for (const item of row.items) {
        const line = document.createElement("li");
        line.textContent = item;
        points.append(line);
      }
      article.append(heading, date, points);
      list.append(article);
    }
    if (!rows.length) list.textContent = "暂无公告";
  }

  async function load(refreshFeed=false) {
    data = await api().release_announcements(refreshFeed);
    button.hidden = !data.version;
  }

  function showCenter() {
    active = null;
    title.textContent = "公告";
    filters.hidden = false;
    refresh.hidden = false;
    center.hidden = true;
    acknowledge.textContent = "关闭";
    status.textContent = "";
    render(data.entries.filter(row => filter.value === "all" || row.kind === filter.value));
    if (!dialog.open) dialog.showModal();
  }

  function showStartup() {
    const row = data.entries.find(item => item.id === data.startup[0]);
    if (!row || dialog.open) return;
    active = row.id;
    title.textContent = row.kind === "release" ? "版本更新" : "作者通知";
    filters.hidden = true;
    refresh.hidden = true;
    center.hidden = false;
    acknowledge.textContent = "知道了";
    status.textContent = "";
    render([row]);
    dialog.showModal();
  }

  async function finish(openCenter) {
    if (finishing) return;
    finishing = true;
    try {
      if (active) {
        const id = active;
        await api().acknowledge_announcement(id);
        data.startup = data.startup.filter(item => item !== id);
        active = null;
      }
      if (openCenter) showCenter();
      else {
        if (dialog.open) dialog.close();
        showStartup();
      }
    } catch {
      status.textContent = "已读状态暂未保存，请再试一次。";
      if (!dialog.open) dialog.showModal();
    } finally {
      finishing = false;
    }
  }

  button.onclick = () => { if (data?.version) showCenter(); };
  acknowledge.onclick = () => finish(false);
  center.onclick = () => finish(true);
  filter.onchange = showCenter;
  refresh.onclick = async () => {
    refresh.disabled = true;
    status.textContent = "正在检查公开公告…";
    try {
      await load(true);
      showCenter();
      status.textContent = data.freshness === "updated" ? "公告已更新" : "已显示本机公告，暂时无法刷新。";
    } catch {
      status.textContent = "已显示本机公告，暂时无法刷新。";
    } finally {
      refresh.disabled = false;
    }
  };
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    finish(false);
  });

  window.pcarAnnouncements = Object.freeze({
    close() {
      if (!dialog.open) return false;
      finish(false);
      return true;
    }
  });

  async function connect() {
    if (connected || !api()?.release_announcements) return;
    connected = true;
    try {
      await load();
      if (data.version) showStartup();
    } catch {
      connected = false;
    }
  }
  window.addEventListener("pywebviewready", connect);
  document.addEventListener("visibilitychange", connect);
  connect();
})();
