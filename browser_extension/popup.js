function $(id) { return document.getElementById(id); }

function send(type, payload = {}) {
  return chrome.runtime.sendMessage({ type, ...payload });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

function renderStatus(st) {
  const dot = $("dot");
  dot.className = "dot";
  if (st.connected && st.helloOk) dot.classList.add("ok");
  else if (st.wsState === "connecting" || st.reconnectScheduled) dot.classList.add("warn");
  else dot.classList.add("bad");

  $("connText").textContent = st.connected && st.helloOk
    ? "已连接并注册"
    : (st.connected ? "WebSocket 已连接，等待注册" : `未连接：${st.wsState || "unknown"}`);

  const active = st.activeTask
    ? `${st.activeTask.provider || ""} / ${st.activeTask.task_id || ""} / ${st.activeTask.started_at || ""}`
    : "无";
  $("statusKv").innerHTML = `
    <div>bridge: ${esc(st.bridgeUrl)}</div>
    <div>space/window: ${esc(st.spaceId)} / ${esc(st.windowKey)}</div>
    <div>client_id: ${esc(st.clientId || "")}</div>
    <div>active_task: ${esc(active)}</div>
    <div>last_event: ${esc(st.lastEventAt || "")}</div>
    <div>last_error: ${esc(st.lastError || "")}</div>
  `;
}

function renderLogs(logs) {
  const root = $("logs");
  if (!logs || !logs.length) {
    root.innerHTML = "<div class='hint'>暂无日志</div>";
    return;
  }
  root.innerHTML = logs.map(x => {
    const data = x.data ? "\n" + JSON.stringify(x.data, null, 2) : "";
    return `<div class="log ${esc(x.level || "")}">
      <span class="ts">${esc(x.ts || "")}</span>
      <span class="lvl">[${esc(x.level || "info")}]</span>
      ${esc(x.message || "")}${esc(data)}
    </div>`;
  }).join("");
}

async function refresh() {
  const resp = await send("popup.getState");
  if (!resp || !resp.ok) {
    $("connText").textContent = "读取 background 状态失败";
    return;
  }
  const cfg = resp.config || {};
  const st = resp.status || {};
  $("bridgeUrl").value = cfg.bridgeUrl || st.bridgeUrl || "";
  $("bridgeToken").value = cfg.bridgeToken || "";
  $("spaceId").value = cfg.spaceId || st.spaceId || "";
  $("windowKey").value = cfg.windowKey || st.windowKey || "";
  $("veoArchiveEnabled").checked = cfg.veoArchiveEnabled !== false;
  renderStatus(st);
  renderLogs(resp.logs || []);
}

async function save() {
  await send("popup.saveConfig", {
    config: {
      bridgeUrl: $("bridgeUrl").value.trim(),
      bridgeToken: $("bridgeToken").value.trim(),
      spaceId: $("spaceId").value.trim(),
      windowKey: $("windowKey").value.trim(),
      veoArchiveEnabled: $("veoArchiveEnabled").checked
    }
  });
  setTimeout(refresh, 300);
}

async function reconnect() {
  await send("popup.reconnect");
  setTimeout(refresh, 300);
}

async function clearLogs() {
  await send("popup.clearLogs");
  await refresh();
}

$("saveBtn").addEventListener("click", save);
$("reconnectBtn").addEventListener("click", reconnect);
$("refreshBtn").addEventListener("click", refresh);
$("clearBtn").addEventListener("click", clearLogs);

refresh();
setInterval(refresh, 2000);
