import { ensureTab as ensureGenericTab, fetchJson, compactErrorResponse } from "./common.js";

const URLS = {
  credits: "https://aisandbox-pa.googleapis.com/v1/credits",
  uploadImage: "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage",
  videoT2V: "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText",
  videoI2VStart: "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage",
  videoI2VStartEnd: "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage",
  videoR2V: "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages",
  videoPoll: "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus",
  upsampleImage: "https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage",
  workflows: "https://aisandbox-pa.googleapis.com/v1/flowWorkflows"
};

function authHeaders(at) {
  return { "Accept": "application/json", "Content-Type": "application/json", "Authorization": `Bearer ${at}` };
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function sessionId() { return `;${Date.now()}`; }
function randSeed(max = 99999) { return 1 + Math.floor(Math.random() * max); }

function timeoutMsFrom(value, fallbackMs, minMs = 1000, maxMs = 120000) {
  const n = Number(value);
  const got = Number.isFinite(n) && n > 0 ? n : fallbackMs;
  return Math.max(minMs, Math.min(maxMs, Math.floor(got)));
}

async function promiseWithTimeout(promise, timeoutMs, label) {
  let timer = null;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label || "operation"} timeout after ${timeoutMs}ms`)), timeoutMs);
      })
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function fetchWithTimeout(url, init, timeoutMs, label) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...(init || {}), signal: controller.signal });
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error(`${label || "fetch"} timeout after ${timeoutMs}ms`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// VEO supports concurrent jobs in the same fingerprint-browser window. When
// several jobs reuse one labs.google tab, job B's submit/done refresh can
// destroy the frame while job A is running a MAIN-world fetch via
// chrome.scripting.executeScript; Chrome may then resolve with an undefined
// result ("pageFetchJson returned empty result ..."). Serialize per-tab
// navigation/refresh with frame-dependent executeScript calls, without locking
// the whole workflow, so video polling and separate jobs can still overlap.
const veoTabOpLocks = new Map();
const VEO_RUN_ID = Symbol("veoRunId");
const activeVeoTaskRuns = new Map();
let veoTaskRunSeq = 0;

function beginVeoTaskRun(msg, runtime) {
  const taskId = String((runtime && runtime.taskId) || (msg && msg.task_id) || "unknown");
  const runId = `${taskId}:${Date.now()}:${++veoTaskRunSeq}`;
  activeVeoTaskRuns.set(runId, {
    task_id: taskId,
    provider: "veo",
    started_at: Date.now()
  });
  if (runtime) {
    try { runtime[VEO_RUN_ID] = runId; } catch (_) {}
  }
  return runId;
}

function endVeoTaskRun(runId, runtime) {
  if (runId) activeVeoTaskRuns.delete(runId);
  if (runtime) {
    try {
      if (runtime[VEO_RUN_ID] === runId) delete runtime[VEO_RUN_ID];
    } catch (_) {}
  }
}

function countOtherActiveVeoTaskRuns(runtime) {
  const currentRunId = runtime && runtime[VEO_RUN_ID];
  let n = 0;
  for (const runId of activeVeoTaskRuns.keys()) {
    if (runId !== currentRunId) n++;
  }
  return n;
}

async function shouldSkipProjectPageRefresh(progress, runtime, stage, url) {
  const otherActiveTasks = countOtherActiveVeoTaskRuns(runtime);
  if (!otherActiveTasks) return false;
  try {
    await runtime.progress(progress, {
      stage: `${stage}_skipped`,
      url,
      reason: "other_tasks_running",
      active_veo_tasks: activeVeoTaskRuns.size,
      other_active_veo_tasks: otherActiveTasks
    });
  } catch (_) {}
  return true;
}

async function withVeoTabOpLock(tabId, label, fn) {
  const key = `veo-tab:${String(tabId || "unknown")}`;
  const prev = veoTabOpLocks.get(key) || Promise.resolve();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const tail = prev.catch(() => {}).then(() => gate);
  veoTabOpLocks.set(key, tail);
  await prev.catch(() => {});
  try {
    return await fn();
  } finally {
    try { release(); } catch (_) {}
    if (veoTabOpLocks.get(key) === tail) veoTabOpLocks.delete(key);
  }
}

function isTransientPageFetchError(e) {
  const s = String((e && e.message) || e || "");
  return /empty result|frame.*(removed|detached|destroyed)|cannot access.*contents|extension context invalidated|no tab with id|tab.*closed|target closed|execution context.*destroyed/i.test(s);
}
function archiveEnabled(p, key = "archive_workflow") {
  const v = p && Object.prototype.hasOwnProperty.call(p, key) ? p[key] : true;
  return v !== false;
}

function isVeoFlowPageUrl(raw) {
  try {
    const u = new URL(String(raw || ""));
    return u.protocol === "https:" && u.hostname === "labs.google" && u.pathname.startsWith("/fx/tools/flow");
  } catch (_) {
    return false;
  }
}

async function getCurrentWindowActiveTab() {
  try {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (Array.isArray(tabs) && tabs[0]) return tabs[0];
  } catch (_) {}
  try {
    const win = await chrome.windows.getLastFocused({ populate: true, windowTypes: ["normal"] });
    const tabs = Array.isArray(win && win.tabs) ? win.tabs : [];
    return tabs.find(t => t && t.active) || tabs[0] || null;
  } catch (_) {}
  try {
    const tabs = await chrome.tabs.query({});
    return (tabs || []).find(t => t && t.active) || (tabs || [])[0] || null;
  } catch (_) {
    return null;
  }
}

async function fetchVeoCurrentPageTask(msg, runtime) {
  const tab = await getCurrentWindowActiveTab();
  const url = String((tab && tab.url) || "");
  try {
    await runtime.progress(100, { stage: "current_page", url, is_flow_page: isVeoFlowPageUrl(url) });
  } catch (_) {}
  return {
    type: "veo_current_page",
    tab_id: tab && tab.id ? tab.id : null,
    window_id: tab && tab.windowId ? tab.windowId : null,
    url,
    title: String((tab && tab.title) || ""),
    is_flow_page: isVeoFlowPageUrl(url),
    required_url_prefix: "https://labs.google/fx/tools/flow"
  };
}

async function waitTabComplete(tabId, timeoutMs = 45000) {
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  while (Date.now() < deadline) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab && tab.status === "complete") return true;
    } catch (_) {}
    await sleep(250);
  }
  return false;
}

async function ensureVeoProjectTab(projectPage, { active = true, navigate = true } = {}) {
  const targetUrl = projectPage || "https://labs.google/fx";
  const tabs = await chrome.tabs.query({});
  const exact = tabs.find(t => (t.url || "") === targetUrl);
  const found = exact || tabs.find(t => (t.url || "").startsWith("https://labs.google/"));
  if (found && found.id) {
    if (navigate && targetUrl && found.url !== targetUrl) {
      await withVeoTabOpLock(found.id, "ensure_project_tab_navigate", async () => {
        await chrome.tabs.update(found.id, { url: targetUrl, active });
        await waitTabComplete(found.id, 45000);
        await sleep(1200);
      });
    } else {
      await chrome.tabs.update(found.id, { active });
    }
    return found.id;
  }
  const tab = await chrome.tabs.create({ url: targetUrl, active });
  if (tab && tab.id) {
    await waitTabComplete(tab.id, 45000);
    await sleep(1200);
  }
  return tab.id;
}

async function simulateHumanActivity(tabId, runtime, minMs = 3000, maxMs = 10000) {
  const duration = Math.max(1000, Math.floor(minMs + Math.random() * Math.max(1, maxMs - minMs)));
  try {
    await runtime.progress(4, { stage: "human_activity", duration_ms: duration });
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [duration],
      func: async (durationMs) => {
        const sleep = (ms) => new Promise(r => setTimeout(r, ms));
        const end = Date.now() + durationMs;
        let x = Math.max(20, Math.floor(window.innerWidth * (0.2 + Math.random() * 0.6)));
        let y = Math.max(20, Math.floor(window.innerHeight * (0.2 + Math.random() * 0.6)));
        while (Date.now() < end) {
          x = Math.max(5, Math.min(window.innerWidth - 5, x + Math.floor((Math.random() - 0.5) * 180)));
          y = Math.max(5, Math.min(window.innerHeight - 5, y + Math.floor((Math.random() - 0.5) * 120)));
          const el = document.elementFromPoint(x, y) || document.body;
          el.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: x, clientY: y, movementX: Math.floor((Math.random() - 0.5) * 20), movementY: Math.floor((Math.random() - 0.5) * 20) }));
          if (Math.random() < 0.35) window.scrollBy({ top: Math.floor((Math.random() - 0.45) * 420), left: 0, behavior: "smooth" });
          await sleep(180 + Math.random() * 520);
        }
      }
    });
  } catch (_) {}
}

function flowProjectsUrlFrom(projectPage) {
  try {
    const u = new URL(projectPage || "https://labs.google/fx");
    const lang = u.pathname.match(/^\/fx\/([a-z]{2})(?:\/|$)/i);
    return `${u.origin}/fx${lang ? `/${lang[1]}` : ""}/tools/flow`;
  } catch (_) {
    return "https://labs.google/fx/tools/flow";
  }
}

async function recoverFlowErrorPage(tabId, projectPage, runtime, progress, reason = "recover_flow_error_page") {
  try {
    const projectsUrl = flowProjectsUrlFrom(projectPage);
    const frames = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [projectsUrl],
      func: async (fallbackUrl) => {
        const text = document.body ? String(document.body.innerText || "") : "";
        const isError = /Something\s+went\s+wrong/i.test(text) && /Back\s+to\s+projects/i.test(text);
        if (!isError) return { recovered: false, is_error: false, url: location.href };
        const els = Array.from(document.querySelectorAll("button,a,[role='button']"));
        const btn = els.find(el => /Back\s+to\s+projects/i.test(String(el.innerText || el.textContent || "")));
        if (btn) {
          btn.click();
          return { recovered: true, is_error: true, clicked: true, url: location.href };
        }
        location.href = fallbackUrl;
        return { recovered: true, is_error: true, clicked: false, navigated: true, url: location.href };
      }
    });
    const info = Array.isArray(frames) && frames[0] ? frames[0].result : null;
    if (!info || !info.is_error) return false;
    try {
      await runtime.progress(progress, {
        stage: reason,
        clicked: !!info.clicked,
        fallback_url: projectsUrl,
        from_url: info.url || ""
      });
    } catch (_) {}
    await waitTabComplete(tabId, 45000);
    await sleep(1500);
    return true;
  } catch (_) {
    return false;
  }
}

async function reloadProjectPage(progress, tabId, projectPage, runtime) {
  return await withVeoTabOpLock(tabId, "reload_project_page", async () => {
    if (await shouldSkipProjectPageRefresh(progress, runtime, "reload_project_page", projectPage)) return false;
    await runtime.progress(progress, { stage: "reload_project_page", url: projectPage });
    try {
      // 单任务时对 project_page 做一次真实刷新；如果还有其它 VEO
      // 任务在跑，上面的检查会跳过刷新，避免销毁其它任务正在使用的 frame。
      await chrome.tabs.update(tabId, { active: true });
      await chrome.tabs.reload(tabId, { bypassCache: false });
      await waitTabComplete(tabId, 45000);
      await sleep(1200);
      await recoverFlowErrorPage(tabId, projectPage, runtime, progress, "recover_flow_error_page_after_reload");
      return true;
    } catch (e) {
      // reload 失败时兜底导航到项目页，仍保证插件任务从 projectPage 开始。
      await chrome.tabs.update(tabId, { url: projectPage, active: true });
      await waitTabComplete(tabId, 45000);
      await sleep(1200);
      await recoverFlowErrorPage(tabId, projectPage, runtime, progress, "recover_flow_error_page_after_navigate");
      return true;
    }
  });
}

async function pageFetchJson(tabId, url, { method = "GET", headers = {}, body = null, attempts = 3, timeoutMs = 60000 } = {}) {
  let lastErr = null;
  const maxAttempts = Math.max(1, Number.parseInt(attempts, 10) || 1);
  const requestTimeoutMs = timeoutMsFrom(timeoutMs, 60000, 3000, 180000);
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      const result = await promiseWithTimeout(withVeoTabOpLock(tabId, "page_fetch_json", async () => {
        try {
          const tab = await chrome.tabs.get(tabId);
          if (tab && tab.status !== "complete") await waitTabComplete(tabId, 45000);
        } catch (_) {}
        const frames = await chrome.scripting.executeScript({
          target: { tabId },
          world: "MAIN",
          args: [url, { method, headers, body, timeoutMs: requestTimeoutMs }],
          func: async (u, opts) => {
            const controller = new AbortController();
            const timeoutMs = Math.max(1000, Number(opts.timeoutMs || 60000));
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            const init = {
              method: opts.method || "GET",
              headers: opts.headers || {},
              credentials: "include",
              signal: controller.signal
            };
            if (opts.body !== null && opts.body !== undefined) {
              init.body = JSON.stringify(opts.body);
            }
            try {
              const resp = await fetch(u, init);
              const text = await resp.text();
              const hdrs = {};
              try {
                for (const [k, v] of resp.headers.entries()) hdrs[k] = v;
              } catch (_) {}
              let json = null;
              try { json = text ? JSON.parse(text) : null; } catch (_) {}
              return { status: resp.status, headers: hdrs, text, json, url: resp.url };
            } catch (e) {
              if (e && e.name === "AbortError") {
                return { status: 599, headers: {}, text: `request timeout after ${timeoutMs}ms`, json: null, url: u, error: "timeout" };
              }
              throw e;
            } finally {
              clearTimeout(timer);
            }
          }
        });
        return Array.isArray(frames) && frames[0] ? frames[0].result : null;
      }), requestTimeoutMs + 10000, `pageFetchJson ${method} ${url}`);
      if (result) return result;
      lastErr = new Error(`pageFetchJson returned empty result for ${url}`);
    } catch (e) {
      lastErr = e;
    }
    if (attempt + 1 < maxAttempts) {
      const extra = isTransientPageFetchError(lastErr) ? 500 : 0;
      await sleep(extra + 250 * (attempt + 1));
    }
  }
  throw lastErr || new Error(`pageFetchJson returned empty result for ${url}`);
}

async function getAccessTokenFromPage(tabId) {
  const result = await withVeoTabOpLock(tabId, "get_access_token", async () => {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab && tab.status !== "complete") await waitTabComplete(tabId, 45000);
    } catch (_) {}
    const frames = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async () => {
        const tries = [
          "/api/auth/session",
          "/fx/api/auth/session"
        ];
        for (const u of tries) {
          try {
            const r = await fetch(u, { credentials: "include" });
            if (!r.ok) continue;
            const j = await r.json();
            const tok = j && (j.accessToken || j.access_token || j.token);
            if (tok) return { access_token: tok, expires: j.expires || null, email: (j.user && j.user.email) || null };
          } catch (_) {}
        }
        return {};
      }
    });
    return Array.isArray(frames) && frames[0] ? frames[0].result : null;
  });
  if (!result || !result.access_token) throw new Error(`VEO access token not found: ${JSON.stringify(result || {})}`);
  return result;
}

function cookieExpiresToIso(cookie) {
  const exp = Number(cookie && cookie.expirationDate);
  if (!Number.isFinite(exp) || exp <= 0) return null;
  try {
    return new Date(exp * 1000).toISOString();
  } catch (_) {
    return null;
  }
}

function veoCookieUrl(targetUrl) {
  try {
    const u = new URL(targetUrl || "https://labs.google");
    if (!/(\.|^)labs\.google$/i.test(u.hostname)) return "https://labs.google";
    return `${u.origin}`;
  } catch (_) {
    return "https://labs.google";
  }
}

async function getLongAccessTokenFromCookies(targetUrl) {
  const url = veoCookieUrl(targetUrl);
  const baseName = "__Secure-next-auth.session-token";
  let exact = null;
  try {
    exact = await chrome.cookies.get({ url, name: baseName });
  } catch (_) {
    exact = null;
  }
  if (exact && exact.value) {
    return {
      access_token: exact.value,
      session_token: exact.value,
      expires: cookieExpiresToIso(exact),
      cookie_name: exact.name
    };
  }

  let cookies = [];
  try {
    cookies = await chrome.cookies.getAll({ url });
  } catch (_) {
    cookies = [];
  }
  const parts = cookies
    .filter(c => c && typeof c.name === "string" && (c.name === baseName || c.name.startsWith(`${baseName}.`)) && c.value)
    .sort((a, b) => {
      const ai = a.name === baseName ? -1 : Number.parseInt(a.name.slice(baseName.length + 1), 10);
      const bi = b.name === baseName ? -1 : Number.parseInt(b.name.slice(baseName.length + 1), 10);
      return (Number.isFinite(ai) ? ai : 9999) - (Number.isFinite(bi) ? bi : 9999);
    });
  if (!parts.length) {
    throw new Error("未找到 __Secure-next-auth.session-token cookie（请确认窗口已登录 Google/VEO）");
  }
  const token = parts.map(c => String(c.value || "")).join("");
  if (!token) throw new Error("VEO long session-token cookie 为空");
  const expCookie = parts.find(c => Number(c.expirationDate) > 0) || parts[0];
  return {
    access_token: token,
    session_token: token,
    expires: cookieExpiresToIso(expCookie),
    cookie_name: parts.length === 1 ? parts[0].name : `${baseName}.*`,
    cookie_parts: parts.length
  };
}

async function fetchShortAccessTokenByExtensionFetch(targetUrl) {
  const cookieUrl = veoCookieUrl(targetUrl);
  const origin = new URL(cookieUrl).origin;
  const tries = [
    `${origin}/fx/api/auth/session`,
    `${origin}/api/auth/session`
  ];
  let last = "";
  for (const u of tries) {
    try {
      const r = await fetch(u, {
        method: "GET",
        credentials: "include",
        headers: { "Accept": "application/json" }
      });
      const text = await r.text();
      if (!r.ok) {
        last = `HTTP ${r.status} ${text.slice(0, 200)}`;
        continue;
      }
      const j = text ? JSON.parse(text) : {};
      const tok = j && (j.accessToken || j.access_token || j.token);
      if (tok) {
        return {
          access_token: tok,
          expires: j.expires || null,
          email: (j.user && j.user.email) || null
        };
      }
      last = `auth/session missing token: ${JSON.stringify(j).slice(0, 200)}`;
    } catch (e) {
      last = String((e && e.message) || e || "");
    }
  }
  throw new Error(last || "auth/session 未返回 access_token");
}

function cleanTokenValue(v) {
  return String(v || "").trim();
}

async function fetchVeoLongAccessTokenTask(msg, runtime) {
  const p = msg.payload || {};
  const targetUrl = p.target_url || p.project_page || "https://labs.google/fx";
  await runtime.progress(5, { stage: "long_access_token" });
  const longInfo = await getLongAccessTokenFromCookies(targetUrl);
  await runtime.progress(100, { stage: "done", token_kind: "long" });
  return {
    type: "veo_long_access_token",
    ...longInfo,
    source: "extension.cookies"
  };
}

async function fetchVeoShortAccessTokenTask(msg, runtime) {
  const p = msg.payload || {};
  const projectPage = p.project_page || p.target_url || "https://labs.google/fx";
  await runtime.progress(5, { stage: "short_access_token" });
  let shortInfo = null;
  try {
    shortInfo = await fetchShortAccessTokenByExtensionFetch(projectPage);
  } catch (_) {
    const tabId = await ensureVeoProjectTab(projectPage, { navigate: true, active: true });
    shortInfo = await getAccessTokenFromPage(tabId);
  }
  await runtime.progress(100, { stage: "done", token_kind: "short" });
  return {
    type: "veo_short_access_token",
    access_token: shortInfo.access_token,
    expires: shortInfo.expires || null,
    email: shortInfo.email || null,
    source: "extension.auth_session"
  };
}

async function fetchVeoAccessTokensTask(msg, runtime) {
  const p = msg.payload || {};
  const projectPage = p.project_page || p.target_url || "https://labs.google/fx";
  const extSessionToken = cleanTokenValue(p.ext_session_token || p.expected_session_token || p.current_session_token);
  const extShortAccessToken = cleanTokenValue(p.ext_short_access_token || p.short_access_token);
  const extShortExpires = cleanTokenValue(p.ext_short_expires || p.short_expires) || null;
  await runtime.progress(3, { stage: "long_access_token" });
  const longInfo = await getLongAccessTokenFromCookies(projectPage);
  const longSessionToken = cleanTokenValue(longInfo && longInfo.session_token);
  if (!longSessionToken) throw new Error("VEO long session_token not found");

  await runtime.progress(15, {
    stage: "short_access_token",
    session_token_matches_ext: !!(extSessionToken && longSessionToken === extSessionToken)
  });
  let shortInfo = null;
  let shortSource = "extension.fetch";

  try {
    shortInfo = await fetchShortAccessTokenByExtensionFetch(projectPage);
  } catch (e) {
    shortSource = "page.auth_session";
    await runtime.progress(20, { stage: "short_access_token_page_fallback", error: String((e && e.message) || e || "").slice(0, 200) });
    const tabId = await ensureVeoProjectTab(projectPage, { navigate: true, active: true });
    shortInfo = await getAccessTokenFromPage(tabId);
  }
  if (!shortInfo || !shortInfo.access_token) throw new Error("VEO short access_token not found");
  await runtime.progress(100, { stage: "done", token_kind: "long_short" });

  return {
    type: "veo_access_tokens",
    access_token: shortInfo && shortInfo.access_token ? shortInfo.access_token : null,
    session_token: shortInfo && shortInfo.access_token ? shortInfo.access_token : null,
    expires: shortInfo && shortInfo.expires ? shortInfo.expires : null,
    cookie_name: longInfo.cookie_name || null,
    cookie_parts: longInfo.cookie_parts || 1,
    short_access_token: shortInfo && shortInfo.access_token ? shortInfo.access_token : null,
    short_expires: shortInfo && shortInfo.expires ? shortInfo.expires : null,
    email: shortInfo && shortInfo.email ? shortInfo.email : null,
    source: "extension",
    short_source: shortSource,
    ext_session_token_matched: !!(extSessionToken && longSessionToken === extSessionToken),
    ext_session_token_changed: !!(extSessionToken && longSessionToken !== extSessionToken)
  };
}

function normalizeCreditsPayload(data) {
  const credits = Number.parseInt(data && data.credits != null ? data.credits : 0, 10) || 0;
  const tier = data && (data.userPaygateTier || data.user_paygate_tier) || null;
  return { credits, user_paygate_tier: tier ? String(tier) : null, raw: data || null };
}

function localNext0105() {
  const now = new Date();
  const dt = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 1, 5, 0, 0);
  if (now.getTime() > dt.getTime()) dt.setDate(dt.getDate() + 1);
  return dt;
}

function fmtLocal(dt) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`;
}

function parseNextUpdateText(text) {
  const s = String(text || "");
  if (!s.trim()) return null;
  if (/Next\s+update\s*:\s*tomorrow\b/i.test(s)) return fmtLocal(localNext0105());
  const m = s.match(/Next\s+update\s*:\s*([A-Za-z]{3,9})\s+(\d{1,2})(?:\s*,\s*(\d{4}))?/i);
  if (!m) return null;
  const monMap = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
  const mon = monMap[String(m[1] || "").slice(0, 3).toLowerCase()];
  const day = Number.parseInt(m[2], 10);
  if (mon == null || !day || day < 1 || day > 31) return null;
  const now = new Date();
  let year = m[3] ? Number.parseInt(m[3], 10) : now.getFullYear();
  let dt = new Date(year, mon, day, 13, 5, 0, 0);
  if (!m[3] && dt.getTime() < new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()) dt = new Date(year + 1, mon, day, 13, 5, 0, 0);
  if (dt.toDateString() === now.toDateString()) dt = localNext0105();
  return fmtLocal(dt);
}

async function fetchNextUpdateCooldown() {
  try {
    const tabId = await ensureGenericTab("https://one.google.com/ai/activity", "https://one.google.com/ai/activity?g1_landing_page=0", { active: false });
    await sleep(4000);
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => document.body ? document.body.innerText || "" : ""
    });
    return parseNextUpdateText(result || "");
  } catch (_) {
    return null;
  }
}

export async function refreshVeoBalanceTask(msg, runtime) {
  const p = msg.payload || {};
  const projectPage = p.project_page || "https://labs.google/fx";
  await runtime.progress(2, { stage: "ensure_tab", url: projectPage });
  const tabId = await ensureVeoProjectTab(projectPage);
  const tokenInfo = p.access_token ? { access_token: p.access_token, expires: p.access_expires } : await getAccessTokenFromPage(tabId);
  const at = tokenInfo.access_token;
  if (!at) throw new Error("缺少 access_token，无法读取 VEO 余额");
  await runtime.progress(20, { stage: "credits" });
  // 余额接口只依赖 Bearer access_token；优先用扩展自身 fetch，避免在页面 MAIN world
  // executeScript 偶发返回空 result 导致余额刷新失败。仍属于浏览器插件侧读取，不走 CDP。
  let tx = await fetchJson(URLS.credits, { method: "GET", headers: authHeaders(at) });
  if (!tx || !tx.status) {
    tx = await pageFetchJson(tabId, URLS.credits, { method: "GET", headers: authHeaders(at) });
  }
  if (tx.status >= 400) throw new Error(`查询 credits 失败: ${compactErrorResponse(tx)}`);
  const info = normalizeCreditsPayload(tx.json);
  if (p.fetch_cooldown) {
    await runtime.progress(60, { stage: "next_update" });
    const cu = await fetchNextUpdateCooldown();
    if (cu) info.cooldown_until = cu;
    try {
      await ensureVeoProjectTab(projectPage, { navigate: true, active: true });
    } catch (_) {}
  }
  await runtime.progress(100, { stage: "done", credits: info.credits, cooldown_until: info.cooldown_until || null });
  return { type: "veo_balance", ...info };
}

async function getRecaptchaToken(tabId, action) {
  const result = await withVeoTabOpLock(tabId, "get_recaptcha_token", async () => {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab && tab.status !== "complete") await waitTabComplete(tabId, 45000);
    } catch (_) {}
    const frames = await promiseWithTimeout(chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [action],
      func: async (act) => {
        const siteKey = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
        try {
          if (!window.grecaptcha || !window.grecaptcha.enterprise) return "";
          await new Promise(resolve => window.grecaptcha.enterprise.ready(resolve));
          return await window.grecaptcha.enterprise.execute(siteKey, { action: act || "VIDEO_GENERATION" });
        } catch (e) {
          return "";
        }
      }
    }), 20000, `recaptcha ${action || ""}`);
    return Array.isArray(frames) && frames[0] ? frames[0].result : "";
  });
  return String(result || "");
}

async function downloadImageAsBase64(url) {
  const resp = await fetchWithTimeout(url, { credentials: "omit" }, 45000, "download image");
  if (!resp.ok) throw new Error(`download image failed: HTTP ${resp.status}`);
  const blob = await resp.blob();
  const mime = blob.type || "image/jpeg";
  const buf = await blob.arrayBuffer();
  let bin = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return { base64: btoa(bin), mime };
}

async function uploadImage(tabId, url, at, projectId, runtime, index, total) {
  await runtime.progress(7 + index, {
    stage: "upload_image_download",
    index: index + 1,
    total,
    url: String(url || "").slice(0, 300)
  });
  const img = await downloadImageAsBase64(url);
  const ext = img.mime.includes("png") ? "png" : "jpg";
  await runtime.progress(7 + index, {
    stage: "upload_image_to_flow",
    index: index + 1,
    total,
    mime: img.mime,
    bytes_base64: img.base64.length
  });
  const tx = await pageFetchJson(tabId, URLS.uploadImage, {
    method: "POST",
    headers: authHeaders(at),
    body: {
      clientContext: { tool: "PINHOLE", projectId: String(projectId) },
      fileName: `fpbrowser2api_veo_ext_${Date.now()}_${index}.${ext}`,
      imageBytes: img.base64,
      isHidden: false,
      isUserUploaded: true,
      mimeType: img.mime
    },
    attempts: 1,
    timeoutMs: 60000
  });
  if (tx.status >= 400) throw new Error(`VEO upload image failed: ${compactErrorResponse(tx)}`);
  const media = tx.json?.media || {};
  const mediaId = media.name || tx.json?.mediaGenerationId?.mediaGenerationId || tx.json?.mediaGenerationId;
  if (!mediaId) throw new Error(`VEO upload missing mediaId: ${JSON.stringify(tx.json).slice(0, 500)}`);
  await runtime.progress(8 + index, {
    stage: "upload_image_done",
    index: index + 1,
    total,
    media_id: String(mediaId || "").slice(0, 120)
  });
  return {
    mediaId,
    workflowId: media.workflowId || tx.json?.workflow?.name || "",
    projectId: media.projectId || tx.json?.workflow?.projectId || projectId
  };
}

function firstStringByKey(obj, key) {
  if (!obj || typeof obj !== "object") return "";
  if (Object.prototype.hasOwnProperty.call(obj, key) && typeof obj[key] === "string" && obj[key]) return obj[key];
  for (const v of Object.values(obj)) {
    if (v && typeof v === "object") {
      const got = firstStringByKey(v, key);
      if (got) return got;
    }
  }
  return "";
}

function parseImageResult(resp) {
  const mediaList = Array.isArray(resp?.media) ? resp.media : (Array.isArray(resp?.responses?.[0]?.media) ? resp.responses[0].media : []);
  const m0 = mediaList.find(x => x && typeof x === "object");
  if (!m0) throw new Error(`VEO image result empty: ${JSON.stringify(resp).slice(0, 500)}`);
  const fifeUrl = m0.image?.generatedImage?.fifeUrl || firstStringByKey(m0, "fifeUrl");
  if (!fifeUrl) throw new Error(`VEO image missing fifeUrl: ${JSON.stringify(resp).slice(0, 500)}`);
  return {
    fifeUrl,
    mediaName: m0.name || "",
    workflowId: m0.workflowId || m0.image?.generatedImage?.workflowId || "",
    projectId: m0.projectId || ""
  };
}

function parseVideoPoll(resp) {
  let workflowId = "", projectId = "", status = "", videoUrl = "";
  const media = Array.isArray(resp?.media) ? resp.media : [];
  for (const item of media) {
    workflowId ||= item.workflowId || "";
    projectId ||= item.projectId || "";
    status ||= item.mediaMetadata?.mediaStatus?.mediaGenerationStatus || (item.operation?.done ? "DONE" : "");
    videoUrl ||= item.mediaMetadata?.video?.fifeUrl || firstStringByKey(item, "fifeUrl");
  }
  videoUrl ||= firstStringByKey(resp, "fifeUrl");
  const errorMessage = firstStringByKey(resp, "message") || firstStringByKey(resp, "errorMessage") || "";
  return { workflowId, projectId, status, videoUrl, errorMessage };
}

function isTerminalVideoFailureStatus(status) {
  const s = String(status || "").toUpperCase();
  return (
    s.includes("FAILED") ||
    s.includes("FAILURE") ||
    s.includes("CANCELLED") ||
    s.includes("CANCELED") ||
    s.includes("ERROR") ||
    s.includes("REJECTED")
  );
}

function isTerminalVideoDoneWithoutUrlStatus(status) {
  const s = String(status || "").toUpperCase();
  return s === "DONE" || s.includes("SUCCEEDED") || s.includes("SUCCESSFUL") || s.includes("COMPLETE");
}

function normalizeVideoOperations(mediaList) {
  const out = [];
  for (const item of Array.isArray(mediaList) ? mediaList : []) {
    if (!item || typeof item !== "object") continue;
    // batchCheckAsyncVideoGenerationStatus 的 operations[] 只接受 submit 返回的
    // operation 包装对象；submit 返回的 media 项有时会额外带 name/projectId/
    // workflowId/mediaMetadata/video 等字段，原样传会触发 "Unknown name ... at
    // operations[0]"。因此这里按最小字段清洗。
    if (item.operation && typeof item.operation === "object") {
      out.push({ operation: item.operation });
      continue;
    }
    if (typeof item.operation === "string" && item.operation) {
      out.push({ operation: { name: item.operation } });
      continue;
    }
    if (typeof item.name === "string" && item.name) {
      out.push({ operation: { name: item.name } });
    }
  }
  return out;
}

async function archiveWorkflow(tabId, at, workflowId, projectId) {
  if (!workflowId) return false;
  try {
    const url = `${URLS.workflows}/${encodeURIComponent(workflowId)}`;
    const tx = await pageFetchJson(tabId, url, {
      method: "PATCH",
      headers: authHeaders(at),
      body: {
        workflow: {
          name: workflowId,
          projectId: String(projectId || ""),
          metadata: { archived: true }
        },
        updateMask: "metadata.archived"
      }
    });
    return tx.status < 400;
  } catch (_) {
    return false;
  }
}

async function archiveUploadedWorkflows(tabId, at, uploaded, runtime, reason = "cleanup_uploaded_workflows") {
  const items = Array.isArray(uploaded) ? uploaded : [];
  let archived = 0;
  let total = 0;
  for (const up of items) {
    if (!up || !up.workflowId) continue;
    total++;
    if (await archiveWorkflow(tabId, at, up.workflowId, up.projectId)) archived++;
  }
  if (total) {
    try {
      await runtime.progress(12, { stage: reason, archived, total });
    } catch (_) {}
  }
  return { archived, total };
}

async function refreshProjectPageAfterArchive(progress, tabId, projectPage, runtime, reason = "refresh_project_page_after_archive") {
  const url = String(projectPage || "").trim();
  if (!url) return false;
  return await withVeoTabOpLock(tabId, reason, async () => {
    if (await shouldSkipProjectPageRefresh(progress, runtime, reason, url)) return false;
    try {
      await runtime.progress(progress, { stage: reason, url });
    } catch (_) {}
    try {
      await chrome.tabs.update(tabId, { url, active: true });
      await waitTabComplete(tabId, 45000);
      await sleep(1200);
      await recoverFlowErrorPage(tabId, url, runtime, progress, `${reason}_recover_error_page`);
      return true;
    } catch (_) {
      try {
        await chrome.tabs.reload(tabId, { bypassCache: false });
        await waitTabComplete(tabId, 45000);
        await sleep(1200);
        await recoverFlowErrorPage(tabId, url, runtime, progress, `${reason}_recover_error_page_after_reload`);
        return true;
      } catch (_e) {
        return false;
      }
    }
  });
}

async function fetchWorkflowList(tabId, at, projectId) {
  const urls = [
    `${URLS.workflows}?projectId=${encodeURIComponent(projectId)}`,
    `${URLS.workflows}?project_id=${encodeURIComponent(projectId)}`,
    `${URLS.workflows}?parent=${encodeURIComponent(projectId)}`
  ];
  for (const url of urls) {
    try {
      const tx = await pageFetchJson(tabId, url, { method: "GET", headers: authHeaders(at) });
      if (tx.status < 400 && tx.json) return tx.json;
    } catch (_) {}
  }
  return null;
}

function pickLatestWorkflowFromList(resp) {
  const arr = Array.isArray(resp?.workflows) ? resp.workflows
    : Array.isArray(resp?.workflow) ? resp.workflow
    : Array.isArray(resp?.items) ? resp.items
    : [];
  let best = null;
  let bestTs = 0;
  for (const item of arr) {
    if (!item || typeof item !== "object") continue;
    const wid = item.name || item.workflowId || item.id || "";
    if (!wid) continue;
    const ts = Date.parse(item.metadata?.createTime || item.createTime || item.updateTime || 0) || 0;
    if (ts >= bestTs) {
      bestTs = ts;
      best = item;
    }
  }
  if (!best) return null;
  return {
    workflowId: best.name || best.workflowId || best.id || "",
    projectId: best.projectId || best.metadata?.projectId || "",
    archived: !!best.metadata?.archived
  };
}

async function recoverWorkflowAfterEmptySubmit(tabId, at, projectId, runtime, kind) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    await sleep(1500);
    const resp = await fetchWorkflowList(tabId, at, projectId);
    const picked = pickLatestWorkflowFromList(resp);
    if (picked && picked.workflowId) {
      await runtime.progress(95, { stage: "recovered_workflow", workflow_id: picked.workflowId, workflow_kind: kind });
      return picked;
    }
  }
  return null;
}

async function fetchVeoUserPaygateTier(tabId, at) {
  try {
    const tx = await fetchJson(URLS.credits, { method: "GET", headers: authHeaders(at) });
    if (tx && tx.status < 400) return normalizeCreditsPayload(tx.json).user_paygate_tier || "PAYGATE_TIER_NOT_PAID";
  } catch (_) {}
  return "PAYGATE_TIER_NOT_PAID";
}

function normalizePaygateTier(tier) {
  const s = String(tier || "").trim();
  if (["PAYGATE_TIER_NOT_PAID", "PAYGATE_TIER_ONE", "PAYGATE_TIER_TWO"].includes(s)) return s;
  return "PAYGATE_TIER_NOT_PAID";
}

async function upsampleImage2K(tabId, at, p, parsed, runtime) {
  const maxRetries = 2;
  let lastErr = "";
  for (let i = 0; i < maxRetries; i++) {
    const recaptcha = p.recaptcha_token || p.veo_recaptcha_token || p.recaptchaContextToken || await getRecaptchaToken(tabId, "IMAGE_GENERATION");
    if (!recaptcha) {
      lastErr = "no recaptcha token";
      await sleep(1500);
      continue;
    }
    const tier = normalizePaygateTier(p.user_paygate_tier || p.userPaygateTier || await fetchVeoUserPaygateTier(tabId, at));
    const body = {
      mediaId: String(parsed.mediaName || "").trim(),
      targetResolution: "UPSAMPLE_IMAGE_RESOLUTION_2K",
      clientContext: {
        recaptchaContext: { token: recaptcha, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" },
        sessionId: sessionId(),
        projectId: String(p.project_id || parsed.projectId || ""),
        tool: "PINHOLE",
        userPaygateTier: tier
      }
    };
    await runtime.progress(72, { stage: "upsample_image", target_resolution: "2K", attempt: i + 1, user_paygate_tier: tier });
    let ux = null;
    try {
      // upsample 不创建新工作流，遇到并发刷新导致的 transient 空 result 可以安全重试。
      ux = await pageFetchJson(tabId, URLS.upsampleImage, { method: "POST", headers: authHeaders(at), body, attempts: 3 });
    } catch (e) {
      lastErr = String((e && e.message) || e || "");
      // 如果仍然被外部/手动刷新打断，最后再走一次扩展 Service Worker fetch，
      // 它不依赖页面 frame，能避开 MAIN world 被销毁的问题。
      try {
        const fx = await fetchJson(URLS.upsampleImage, { method: "POST", headers: authHeaders(at), body });
        if (fx) ux = fx;
      } catch (e2) {
        lastErr = `${lastErr || "page fetch failed"}; extension fetch: ${String((e2 && e2.message) || e2 || "")}`;
      }
    }
    if (!ux) {
      await sleep(1500);
      continue;
    }
    const enc = ux.json?.encodedImage || "";
    if (ux.status < 400 && enc) return { encodedImage: enc, userPaygateTier: tier };
    lastErr = compactErrorResponse(ux) || `status=${ux && ux.status}`;
    await sleep(1500);
  }
  return { encodedImage: "", error: lastErr };
}

async function runImageWorkflow(tabId, p, at, runtime) {
  const projectId = p.project_id;
  const prompt = p.prompt || "";
  const imageUrls = p.extension_image_reference_urls || [];
  const imageInputs = [];
  const uploaded = [];
  for (let i = 0; i < imageUrls.length; i++) {
    const up = await uploadImage(tabId, imageUrls[i], at, projectId, runtime, i, imageUrls.length);
    uploaded.push(up);
    imageInputs.push({ name: up.mediaId, imageInputType: "IMAGE_INPUT_TYPE_REFERENCE" });
  }
  await runtime.progress(10, { stage: "submit_image_task", workflow_kind: "image" });
  const submitUrl = `https://aisandbox-pa.googleapis.com/v1/projects/${encodeURIComponent(projectId)}/flowMedia:batchGenerateImages`;
  let tx = null;
  let parsed = null;
  let submitErr = "";
  const maxImageSubmitAttempts = 2; // 首次提交 + 失败后连续重试 2 次
  for (let attempt = 0; attempt < maxImageSubmitAttempts; attempt++) {
    try {
      const recaptcha = await getRecaptchaToken(tabId, "IMAGE_GENERATION");
      if (!recaptcha) throw new Error("VEO image recaptcha token not found");
      const clientContext = {
        recaptchaContext: { token: recaptcha, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" },
        sessionId: sessionId(),
        projectId: String(projectId),
        tool: "PINHOLE"
      };
      const body = {
        clientContext,
        mediaGenerationContext: { batchId: crypto.randomUUID() },
        useNewMedia: true,
        requests: [{
          clientContext,
          seed: randSeed(999999),
          imageModelName: p.extension_image_model_name || "NARWHAL",
          imageAspectRatio: p.extension_image_aspect_ratio || "IMAGE_ASPECT_RATIO_LANDSCAPE",
          structuredPrompt: { parts: [{ text: prompt }] },
          imageInputs
        }]
      };
      await runtime.progress(10, { stage: "submit_image_task", workflow_kind: "image", attempt: attempt + 1, max_attempts: maxImageSubmitAttempts });
      tx = await pageFetchJson(tabId, submitUrl, { method: "POST", headers: authHeaders(at), body, attempts: 1 });
      if (tx.status >= 400) throw new Error(`VEO image submit failed: ${compactErrorResponse(tx)}`);
      parsed = parseImageResult(tx.json);
      break;
    } catch (e) {
      submitErr = String(e && e.message ? e.message : e || "");
      if (/empty result|result null|missing fifeUrl|result undefined/i.test(submitErr)) {
        const recovered = await recoverWorkflowAfterEmptySubmit(tabId, at, projectId, runtime, "image");
        if (recovered && recovered.workflowId) {
          const finalUrl = await fetchWorkflowList(tabId, at, projectId);
          const latest = pickLatestWorkflowFromList(finalUrl) || recovered;
          parsed = {
            fifeUrl: "",
            mediaName: latest.workflowId,
            workflowId: latest.workflowId,
            projectId: latest.projectId || projectId
          };
          break;
        }
      }
      if (attempt + 1 < maxImageSubmitAttempts) {
        await runtime.progress(10, {
          stage: "submit_image_retry",
          workflow_kind: "image",
          attempt: attempt + 1,
          next_attempt: attempt + 2,
          max_attempts: maxImageSubmitAttempts,
          error: submitErr.slice(0, 300)
        });
        await sleep(500 * (attempt + 1));
      }
    }
  }
  if (!parsed) {
    if (archiveEnabled(p, "archive_uploaded_workflows")) await archiveUploadedWorkflows(tabId, at, uploaded, runtime, "cleanup_uploaded_workflows_submit_failed");
    await refreshProjectPageAfterArchive(98, tabId, p.project_page, runtime);
    throw new Error(submitErr || "VEO image submit failed");
  }
  let shareUrl = parsed.fifeUrl;
  let originImageUrl = parsed.fifeUrl;
  let resLabel = p.extension_image_resolution_label || "1K";
  let upsampleOk = false;
  let upsampleError = "";
  if (p.extension_image_want_2k && parsed.mediaName) {
    const up = await upsampleImage2K(tabId, at, p, parsed, runtime);
    if (up.encodedImage) {
      shareUrl = `data:image/jpeg;base64,${up.encodedImage}`;
      upsampleOk = true;
      resLabel = "2K";
    } else {
      upsampleError = up.error || "upsample returned empty encodedImage";
      resLabel = "1K";
    }
  }
  const archived = archiveEnabled(p, "archive_workflow") ? await archiveWorkflow(tabId, at, parsed.workflowId, parsed.projectId || projectId) : false;
  if (archiveEnabled(p, "archive_uploaded_workflows")) await archiveUploadedWorkflows(tabId, at, uploaded, runtime, "cleanup_uploaded_workflows_done");
  await refreshProjectPageAfterArchive(98, tabId, p.project_page, runtime);
  await runtime.progress(100, { stage: "done", image_url: shareUrl, workflow_id: parsed.workflowId });
  return {
    type: "veo_workflow_image",
    message: imageUrls.length ? "VEO 图生图完成" : "VEO 文生图完成",
    workflow_kind: "image",
    share_url: shareUrl,
    image_url: shareUrl,
    origin_image_url: originImageUrl,
    model_name: p.extension_image_model_name || "NARWHAL",
    aspect_ratio: p.extension_image_aspect_ratio || "IMAGE_ASPECT_RATIO_LANDSCAPE",
    resolution: resLabel,
    upsample_ok: upsampleOk,
    upsample_error: upsampleError || undefined,
    project_id: projectId,
    generated_media_id: parsed.mediaName,
    generated_workflow_id: parsed.workflowId,
    workflow_archived: archived,
    i2i_image_count: imageUrls.length
  };
}

async function pollVideo(tabId, at, operations, runtime, p) {
  const maxWait = Math.max(60, Number(p.max_wait_seconds || p.timeout_seconds || 600));
  const interval = Math.max(0.5, Number(p.poll_interval_seconds || 5));
  const mode = p.video_mode || "t2v";
  const modelKey = p.extension_model_key || "";
  const aspectRatio = p.extension_video_aspect_ratio || "";
  const deadline = Date.now() + maxWait * 1000;
  let last = {};
  let attempt = 0;
  while (Date.now() < deadline) {
    await sleep(interval * 1000);
    attempt++;
    const tx = await pageFetchJson(tabId, URLS.videoPoll, {
      method: "POST",
      headers: authHeaders(at),
      body: { operations },
      attempts: 1,
      timeoutMs: 45000
    });
    if (tx.status >= 400) throw new Error(`VEO video poll failed: ${compactErrorResponse(tx)}`);
    last = parseVideoPoll(tx.json);
    const pct = 25 + Math.min(70, Math.floor((Date.now() - (deadline - maxWait * 1000)) / (maxWait * 1000) * 70));
    await runtime.progress(pct, {
      stage: "polling",
      attempt,
      video_mode: mode,
      model_key: modelKey,
      aspect_ratio: aspectRatio,
      status: last.status,
      workflow_id: last.workflowId,
      has_video_url: !!last.videoUrl
    });
    if (last.videoUrl) return last;
    if (isTerminalVideoFailureStatus(last.status)) {
      throw new Error(`VEO video generation failed upstream: status=${last.status || "unknown"} message=${last.errorMessage || ""}`.trim());
    }
    if (isTerminalVideoDoneWithoutUrlStatus(last.status) && !last.videoUrl) {
      throw new Error(`VEO video generation finished without video url: status=${last.status || "unknown"} workflow_id=${last.workflowId || ""}`);
    }
  }
  throw new Error(`VEO video polling timeout; last=${JSON.stringify(last).slice(0, 300)}`);
}

function stripI2vFl(modelKey) {
  return String(modelKey || "").replace("_fl_", "_").replace(/_fl$/, "");
}

async function runVideoWorkflow(tabId, p, at, runtime) {
  const projectId = p.project_id;
  const prompt = p.prompt || "";
  const mode = p.video_mode || "t2v";
  const uploaded = [];
  let submitUrl = URLS.videoT2V;
  let reqItem;
  const aspectRatio = p.extension_video_aspect_ratio || "VIDEO_ASPECT_RATIO_LANDSCAPE";
  let modelKey = p.extension_model_key || "veo_3_1_t2v_fast";

  try {
    await runtime.progress(6, {
      stage: "video_workflow_prepare",
      video_mode: mode,
      model_key: modelKey,
      aspect_ratio: aspectRatio,
      ref_count: mode === "r2v" ? (p.ingredients_urls || []).length : (mode === "i2v" ? (p.i2v_urls || []).length : 0)
    });
    if (mode === "r2v") {
      const refs = [];
      const urls = p.ingredients_urls || [];
      for (let i = 0; i < urls.length; i++) {
        const up = await uploadImage(tabId, urls[i], at, projectId, runtime, i, urls.length);
        uploaded.push(up);
        refs.push({ imageUsageType: "IMAGE_USAGE_TYPE_ASSET", mediaId: up.mediaId });
      }
      submitUrl = URLS.videoR2V;
      reqItem = {
        aspectRatio, seed: randSeed(),
        textInput: { structuredPrompt: { parts: [{ text: prompt }] } },
        videoModelKey: modelKey,
        referenceImages: refs,
        metadata: { sceneId: crypto.randomUUID() }
      };
    } else if (mode === "i2v") {
      const urls = p.i2v_urls || [];
      if (!urls.length) throw new Error("VEO i2v missing image urls");
      const ids = [];
      for (let i = 0; i < urls.length; i++) {
        const up = await uploadImage(tabId, urls[i], at, projectId, runtime, i, urls.length);
        uploaded.push(up);
        ids.push(up.mediaId);
      }
      if (ids[1]) {
        submitUrl = URLS.videoI2VStartEnd;
        reqItem = { aspectRatio, seed: randSeed(), textInput: { prompt }, videoModelKey: modelKey, startImage: { mediaId: ids[0] }, endImage: { mediaId: ids[1] }, metadata: { sceneId: crypto.randomUUID() } };
      } else {
        submitUrl = URLS.videoI2VStart;
        modelKey = stripI2vFl(modelKey);
        reqItem = { aspectRatio, seed: randSeed(), textInput: { prompt }, videoModelKey: modelKey, startImage: { mediaId: ids[0] }, metadata: { sceneId: crypto.randomUUID() } };
      }
    } else {
      reqItem = { aspectRatio, seed: randSeed(), textInput: { prompt }, videoModelKey: modelKey, metadata: { sceneId: crypto.randomUUID() } };
    }

  await runtime.progress(10, { stage: "submit_task", video_mode: mode });
  let operations = [];
  let submitErr = "";
  const maxVideoSubmitAttempts = 2; // 首次提交 + 失败后连续重试 2 次
  for (let attempt = 0; attempt < maxVideoSubmitAttempts; attempt++) {
    try {
      const recaptcha = await getRecaptchaToken(tabId, "VIDEO_GENERATION");
      if (!recaptcha) throw new Error("VEO video recaptcha token not found");
      const clientContext = {
        recaptchaContext: { token: recaptcha, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" },
        sessionId: sessionId(),
        projectId: String(projectId),
        tool: "PINHOLE",
        userPaygateTier: p.user_paygate_tier || p.userPaygateTier || "PAYGATE_TIER_NOT_PAID"
      };
      const body = mode === "r2v"
        ? { mediaGenerationContext: { batchId: crypto.randomUUID() }, clientContext, requests: [reqItem], useV2ModelConfig: true }
        : { clientContext, requests: [reqItem] };
      await runtime.progress(10, { stage: "submit_task", video_mode: mode, attempt: attempt + 1, max_attempts: maxVideoSubmitAttempts });
      const tx = await pageFetchJson(tabId, submitUrl, {
        method: "POST",
        headers: authHeaders(at),
        body,
        attempts: 1,
        timeoutMs: 90000
      });
      if (tx.status >= 400) throw new Error(`VEO video submit failed: ${compactErrorResponse(tx)}`);
      const submitMedia = Array.isArray(tx.json?.media) ? tx.json.media : [];
      operations = normalizeVideoOperations(submitMedia);
      if (!operations.length) throw new Error(`VEO video submit missing operations: ${JSON.stringify(tx.json).slice(0, 500)}`);
      await runtime.progress(15, {
        stage: "video_submit_ok",
        video_mode: mode,
        model_key: modelKey,
        aspect_ratio: aspectRatio,
        operations: operations.length
      });
      break;
    } catch (e) {
      submitErr = String(e && e.message ? e.message : e || "");
      if (attempt + 1 < maxVideoSubmitAttempts) {
        await runtime.progress(10, {
          stage: "submit_video_retry",
          video_mode: mode,
          attempt: attempt + 1,
          next_attempt: attempt + 2,
          max_attempts: maxVideoSubmitAttempts,
          error: submitErr.slice(0, 300)
        });
        await sleep(500 * (attempt + 1));
      }
    }
  }
  if (!operations.length) throw new Error(submitErr || "VEO video submit failed");
  await runtime.progress(25, { stage: "polling", video_mode: mode, model_key: modelKey, aspect_ratio: aspectRatio, operations: operations.length });
  const done = await pollVideo(tabId, at, operations, runtime, p);
  const archived = archiveEnabled(p, "archive_workflow") ? await archiveWorkflow(tabId, at, done.workflowId, done.projectId || projectId) : false;
  if (archiveEnabled(p, "archive_uploaded_workflows")) await archiveUploadedWorkflows(tabId, at, uploaded, runtime, "cleanup_uploaded_workflows_done");
  await refreshProjectPageAfterArchive(98, tabId, p.project_page, runtime);
  await runtime.progress(100, { stage: "done", video_mode: mode, model_key: modelKey, aspect_ratio: aspectRatio, video_url: done.videoUrl, workflow_id: done.workflowId });
  return {
    type: "veo_workflow_video",
    message: mode === "r2v" ? "VEO Ingredients（多图参考）视频完成" : (mode === "i2v" ? "VEO 图生视频完成" : "VEO 文生视频完成"),
    share_url: done.videoUrl,
    thumb_url: (mode === "i2v" ? (p.i2v_urls || [])[0] : (mode === "r2v" ? (p.ingredients_urls || [])[0] : "")) || "",
    video_type: mode,
    model_key: modelKey,
    aspect_ratio: aspectRatio,
    project_id: projectId,
    generated_workflow_id: done.workflowId,
    workflow_archived: archived
  };
  } catch (e) {
    if (archiveEnabled(p, "archive_uploaded_workflows")) await archiveUploadedWorkflows(tabId, at, uploaded, runtime, "cleanup_uploaded_workflows_video_failed");
    await refreshProjectPageAfterArchive(98, tabId, p.project_page, runtime);
    throw e;
  }
}

export async function runVeoTask(msg, runtime) {
  const veoRunId = beginVeoTaskRun(msg, runtime);
  try {
    const p = msg.payload || {};
    const action = String(p.action || p.workflow_kind || "").trim();
    if (action === "current_page" || action === "get_current_page" || action === "current_url" || action === "get_current_url") {
      return await fetchVeoCurrentPageTask(msg, runtime);
    }
    if (action === "fetch_tokens" || action === "fetch_access_tokens" || action === "get_access_tokens") {
      return await fetchVeoAccessTokensTask(msg, runtime);
    }
    try {
      const got = await chrome.storage.local.get(["veo_archive_enabled"]);
      const enabled = got.veo_archive_enabled !== false; // default enabled
      p.archive_workflow = enabled;
      p.archive_uploaded_workflows = enabled;
      await runtime.progress(1, { stage: "archive_setting", archive_enabled: enabled });
    } catch (_) {
      p.archive_workflow = true;
      p.archive_uploaded_workflows = true;
    }
    if (p.workflow_kind === "balance_refresh" || p.action === "refresh_balance") {
      return await refreshVeoBalanceTask(msg, runtime);
    }
    const projectPage = p.project_page || "https://labs.google/fx";
    await runtime.progress(2, { stage: "ensure_tab", url: projectPage });
    // 普通生成任务必须保持在 project_page；如果余额刷新打开了 one.google 标签，
    // 这里会重新选中/导航回精确项目页，避免停留到 /tools/flow 列表页。
    const tabId = await ensureVeoProjectTab(projectPage, { navigate: true, active: true });
    await reloadProjectPage(3, tabId, projectPage, runtime);
    await simulateHumanActivity(tabId, runtime, 3000, 10000);
    await runtime.progress(5, { stage: "access_token" });
    const tokenInfo = p.access_token ? { access_token: p.access_token, expires: p.access_expires } : await getAccessTokenFromPage(tabId);
    const at = tokenInfo.access_token;
    if (p.workflow_kind === "image" || p.image_mode) {
      return await runImageWorkflow(tabId, p, at, runtime);
    }
    return await runVideoWorkflow(tabId, p, at, runtime);
  } finally {
    endVeoTaskRun(veoRunId, runtime);
  }
}
