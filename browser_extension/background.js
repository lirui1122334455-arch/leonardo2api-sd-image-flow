import { runVeoTask } from "./providers/veo_provider.js";
import { runDreaminaTask } from "./providers/dreamina_provider.js";
import { runGptTask } from "./providers/gpt_provider.js";

let ws = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let currentConnectKey = "";
let connectSeq = 0;
const HEARTBEAT_INTERVAL_MS = 15000;
let status = {
  bridgeUrl: "",
  spaceId: "",
  windowKey: "",
  wsState: "init",
  connected: false,
  helloOk: false,
  clientId: "",
  lastError: "",
  lastEventAt: null,
  reconnectScheduled: false,
  activeTask: null
};

async function pushLog(level, message, data = null) {
  const row = {
    ts: new Date().toISOString(),
    level,
    message,
    data
  };
  try {
    const got = await chrome.storage.local.get(["debug_logs"]);
    const logs = Array.isArray(got.debug_logs) ? got.debug_logs : [];
    logs.unshift(row);
    await chrome.storage.local.set({ debug_logs: logs.slice(0, 80) });
  } catch (_) {}
}

async function setStatus(patch) {
  status = { ...status, ...patch, lastEventAt: new Date().toISOString() };
  status.connected = status.wsState === "open";
  try {
    await chrome.storage.local.set({ runtime_status: status });
    const color = status.connected ? "#16a34a" : (status.wsState === "connecting" ? "#f59e0b" : "#dc2626");
    await chrome.action.setBadgeText({ text: status.connected ? "ON" : "OFF" });
    await chrome.action.setBadgeBackgroundColor({ color });
  } catch (_) {}
}

async function getConfig() {
  const cfg = await chrome.storage.local.get([
    "bridge_url",
    "bridge_token",
    "space_id",
    "window_key",
    "veo_archive_enabled"
  ]);
  return {
    bridgeUrl: cfg.bridge_url || "",
    bridgeToken: cfg.bridge_token || "",
    spaceId: cfg.space_id || "",
    windowKey: cfg.window_key || "",
    veoArchiveEnabled: cfg.veo_archive_enabled !== false
  };
}

async function send(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) throw new Error("bridge websocket is not open");
  ws.send(JSON.stringify(obj));
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function startHeartbeat(socket, seq) {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    try {
      if (ws !== socket || seq !== connectSeq || !socket || socket.readyState !== WebSocket.OPEN) {
        stopHeartbeat();
        return;
      }
      socket.send(JSON.stringify({ type: "heartbeat", ts: Date.now() }));
    } catch (e) {
      stopHeartbeat();
      try { socket.close(); } catch (_) {}
    }
  }, HEARTBEAT_INTERVAL_MS);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function normalizeRedirectUrl(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  try {
    const u = new URL(s);
    if (u.protocol === "http:" || u.protocol === "https:") return u.href;
  } catch (_) {}
  return "";
}

async function waitForBridgeReady(timeoutMs = 5000) {
  const end = Date.now() + Math.max(0, Number(timeoutMs || 0));
  while (Date.now() < end) {
    if (status.wsState === "open" && status.helloOk) return true;
    await sleep(120);
  }
  return status.wsState === "open" && status.helloOk;
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  setStatus({ wsState: "closed", helloOk: false, reconnectScheduled: true }).catch(() => {});
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    setStatus({ reconnectScheduled: false }).catch(() => {});
    connectBridge({ reason: "timer" }).catch(console.error);
  }, 3000);
}

async function ensurePersistentConnection(reason = "keepalive") {
  try {
    const cfg = await getConfig();
    const hasIdentity = !!(String(cfg.spaceId || "").trim() && String(cfg.windowKey || "").trim());
    if (!hasIdentity) {
      await setStatus({
        bridgeUrl: cfg.bridgeUrl,
        spaceId: cfg.spaceId,
        windowKey: cfg.windowKey,
        wsState: "waiting_config",
        helloOk: false,
        lastError: "waiting for fpb config from AccessToken update"
      });
      return;
    }
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    await connectBridge({ reason });
  } catch (e) {
    await pushLog("error", "ensurePersistentConnection failed", { error: String(e && e.message || e), reason });
    scheduleReconnect();
  }
}

async function connectBridge(options = {}) {
  const force = !!options.force;
  const reason = options.reason || "auto";
  const cfg = await getConfig();
  const connectKey = JSON.stringify({
    bridgeUrl: cfg.bridgeUrl,
    bridgeToken: cfg.bridgeToken,
    spaceId: cfg.spaceId,
    windowKey: cfg.windowKey
  });

  if (!force && connectKey === currentConnectKey && ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  const old = ws;
  if (old && (old.readyState === WebSocket.OPEN || old.readyState === WebSocket.CONNECTING)) {
    try { old.__fpb_intentional_close = true; old.close(1000, "reconnect"); } catch (_) {}
  }
  currentConnectKey = connectKey;
  const mySeq = ++connectSeq;

  let url = cfg.bridgeUrl;
  if (cfg.bridgeToken) {
    url += (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(cfg.bridgeToken);
  }
  await setStatus({
    bridgeUrl: cfg.bridgeUrl,
    spaceId: cfg.spaceId,
    windowKey: cfg.windowKey,
    wsState: "connecting",
    helloOk: false,
    lastError: "",
    reconnectScheduled: false
  });
  await pushLog("info", "connecting websocket", { url: cfg.bridgeUrl, space_id: cfg.spaceId, window_key: cfg.windowKey, reason });
  const socket = new WebSocket(url);
  ws = socket;
  socket.onopen = async () => {
    if (ws !== socket || mySeq !== connectSeq) return;
    await setStatus({ wsState: "open", lastError: "" });
    await pushLog("info", "websocket open");
    socket.send(JSON.stringify({
      type: "hello",
      version: chrome.runtime.getManifest().version,
      space_id: cfg.spaceId,
      window_key: cfg.windowKey,
      capabilities: ["veo", "veo_tokens", "dreamina", "gpt"]
    }));
    startHeartbeat(socket, mySeq);
  };
  socket.onmessage = async (ev) => {
    if (ws !== socket || mySeq !== connectSeq) return;
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "welcome") {
      await setStatus({ clientId: msg.client_id || status.clientId });
      await pushLog("debug", "bridge welcome", msg);
    } else if (msg.type === "hello.ok") {
      await setStatus({ helloOk: true, clientId: msg.client_id || status.clientId });
      await pushLog("info", "registered with backend", msg);
    } else if (msg.type === "hello.error") {
      await setStatus({ helloOk: false, lastError: msg.message || "hello.error" });
      await pushLog("error", "registration failed", msg);
    }
    if (msg.type === "task.start") {
      await setStatus({ activeTask: { task_id: msg.task_id, provider: msg.provider, started_at: new Date().toISOString() } });
      await pushLog("info", "task.start", { task_id: msg.task_id, provider: msg.provider });
      runTask(msg).catch(async (e) => {
        await setStatus({ activeTask: null, lastError: String(e && e.message || e) });
        await pushLog("error", "task.error", { task_id: msg.task_id, error: String(e && e.message || e) });
        await send({
          type: "task.error",
          task_id: msg.task_id,
          error: { message: String(e && e.message || e), status_code: e.status_code || 502 }
        });
      });
    } else if (msg.type === "ping") {
      await send({ type: "pong" });
    } else if (msg.type === "heartbeat.ok") {
      await setStatus({ lastError: "" });
    }
  };
  socket.onclose = (ev) => {
    if (ws === socket && mySeq === connectSeq) stopHeartbeat();
    if (socket.__fpb_intentional_close || ws !== socket || mySeq !== connectSeq) {
      return;
    }
    setStatus({ wsState: "closed", helloOk: false, lastError: ev.reason || `closed ${ev.code}` }).catch(() => {});
    pushLog("warn", "websocket closed", { code: ev.code, reason: ev.reason }).catch(() => {});
    scheduleReconnect();
  };
  socket.onerror = () => {
    if (ws !== socket || mySeq !== connectSeq) return;
    stopHeartbeat();
    setStatus({ wsState: "error", helloOk: false, lastError: "websocket error" }).catch(() => {});
    pushLog("error", "websocket error").catch(() => {});
    try { socket.close(); } catch (_) {}
  };
}

async function runTask(msg) {
  const taskId = msg.task_id;
  const runtime = {
    taskId,
    progress: async (progress, data = {}) => {
      await pushLog("debug", "task.progress", { task_id: taskId, progress, data });
      await send({ type: "task.progress", task_id: taskId, progress, data });
    }
  };
  let result;
  if (msg.provider === "veo") {
    result = await runVeoTask(msg, runtime);
  } else if (msg.provider === "dreamina") {
    result = await runDreaminaTask(msg, runtime);
  } else if (msg.provider === "gpt") {
    result = await runGptTask(msg, runtime);
  } else {
    throw new Error(`unsupported provider: ${msg.provider}`);
  }
  await pushLog("info", "task.done", { task_id: taskId, result_type: result && result.type });
  await setStatus({ activeTask: null });
  await send({ type: "task.done", task_id: taskId, result });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    const type = message && message.type;
    if (type === "content.fpbConfig") {
      const cfg = message.config || {};
      const redirectUrl = normalizeRedirectUrl(message.redirect_url || cfg.redirect_url || "");
      const patch = {};
      if (cfg.bridge_url) patch.bridge_url = cfg.bridge_url;
      if (cfg.bridge_token) patch.bridge_token = cfg.bridge_token;
      if (cfg.space_id) patch.space_id = cfg.space_id;
      if (cfg.window_key) patch.window_key = cfg.window_key;
      if (Object.keys(patch).length) {
        await chrome.storage.local.set(patch);
        await pushLog("info", "config received from content script", patch);
      }
      await connectBridge({ force: true, reason: "content.fpbConfig" });
      let bridgeReady = false;
      let redirected = false;
      if (redirectUrl && sender && sender.tab && sender.tab.id) {
        bridgeReady = await waitForBridgeReady(5000);
        // 给 Python 侧 trigger 函数留出时间断开 Playwright/CDP，再由插件跳转目标站点。
        await sleep(3000);
        await pushLog("info", "redirecting tab after fpb config and cdp grace delay", { redirect_url: redirectUrl, bridge_ready: bridgeReady, delay_ms: 3000 });
        try {
          await chrome.tabs.update(sender.tab.id, { url: redirectUrl, active: true });
          redirected = true;
        } catch (e) {
          await pushLog("warn", "redirect tab failed", { redirect_url: redirectUrl, error: String(e && e.message || e) });
        }
      }
      sendResponse({ ok: true, redirected, bridge_ready: bridgeReady });
      return;
    }
    if (type === "popup.getState") {
      const cfg = await getConfig();
      const got = await chrome.storage.local.get(["runtime_status", "debug_logs"]);
      sendResponse({
        ok: true,
        config: cfg,
        status: got.runtime_status || status,
        logs: Array.isArray(got.debug_logs) ? got.debug_logs : []
      });
      return;
    }
    if (type === "popup.saveConfig") {
      const cfg = message.config || {};
      await chrome.storage.local.set({
        bridge_url: cfg.bridgeUrl || "",
        bridge_token: cfg.bridgeToken || "",
        space_id: cfg.spaceId || "",
        window_key: cfg.windowKey || "",
        veo_archive_enabled: cfg.veoArchiveEnabled !== false
      });
      await pushLog("info", "config saved from popup");
      connectBridge({ force: true, reason: "popup.saveConfig" }).catch(console.error);
      sendResponse({ ok: true });
      return;
    }
    if (type === "popup.reconnect") {
      await pushLog("info", "manual reconnect");
      connectBridge({ force: true, reason: "popup.reconnect" }).catch(console.error);
      sendResponse({ ok: true });
      return;
    }
    if (type === "popup.clearLogs") {
      await chrome.storage.local.set({ debug_logs: [] });
      sendResponse({ ok: true });
      return;
    }
    sendResponse({ ok: false, error: "unknown message type" });
  })().catch(e => sendResponse({ ok: false, error: String(e && e.message || e) }));
  return true;
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local") return;
  const keys = ["bridge_url", "bridge_token", "space_id", "window_key"];
  const changed = keys.some(k => {
    if (!Object.prototype.hasOwnProperty.call(changes, k)) return false;
    return changes[k].oldValue !== changes[k].newValue;
  });
  if (changed) {
    pushLog("info", "config changed, reconnecting").catch(() => {});
    connectBridge({ force: true, reason: "storage.changed" }).catch(console.error);
  }
});

chrome.runtime.onInstalled.addListener(() => {
  ensurePersistentConnection("runtime.onInstalled").catch(console.error);
});
chrome.runtime.onStartup.addListener(() => {
  ensurePersistentConnection("runtime.onStartup").catch(console.error);
});
try {
  chrome.alarms.create("fpb_keep_ws", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === "fpb_keep_ws") {
      ensurePersistentConnection("alarm.keep_ws").catch(console.error);
    }
  });
} catch (_) {}
ensurePersistentConnection("startup").catch(console.error);
