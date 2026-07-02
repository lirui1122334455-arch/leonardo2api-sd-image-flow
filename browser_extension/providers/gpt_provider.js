const CHATGPT = "https://chatgpt.com";
const WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0";
const WEB_CLIENT_VERSION = "prod-81e0c5cdf6140e8c5db714d613337f4aeab94029";
const WEB_BUILD_NUMBER = "6128297";
const MASK64 = (1n << 64n) - 1n;
const SHA3_RATE_512 = 72;
const GPT_IMAGE2_PUBLIC_MODELS = {
  "gpt-image2-1k": "1k",
  "gpt-image2-2k": "2k",
  "gpt-image2-4k": "4k"
};
const GPT_IMAGE2_MIN_PIXELS = 655360;
const GPT_IMAGE2_MAX_PIXELS = 8294400;
const GPT_IMAGE2_MAX_EDGE = 3840;
const GPT_IMAGE2_SIZE_TABLE = {
  "1k": {
    "1:1": "1024x1024", "3:2": "1216x832", "2:3": "832x1216", "4:3": "1152x864", "3:4": "864x1152",
    "5:4": "1120x896", "4:5": "896x1120", "16:9": "1344x768", "9:16": "768x1344", "21:9": "1536x640"
  },
  "2k": {
    "1:1": "1248x1248", "3:2": "1536x1024", "2:3": "1024x1536", "4:3": "1440x1088", "3:4": "1088x1440",
    "5:4": "1392x1120", "4:5": "1120x1392", "16:9": "1664x928", "9:16": "928x1664", "21:9": "1904x816"
  },
  "4k": {
    "1:1": "2480x2480", "3:2": "3056x2032", "2:3": "2032x3056", "4:3": "2880x2160", "3:4": "2160x2880",
    "5:4": "2784x2224", "4:5": "2224x2784", "16:9": "3312x1872", "9:16": "1872x3312", "21:9": "3808x1632"
  }
};

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function uuid() { const c = globalThis.crypto; return c && c.randomUUID ? c.randomUUID() : `${Date.now()}-${Math.random()}`; }
function uniq(arr) { return [...new Set((arr || []).filter(Boolean))]; }
function gcd(a, b) {
  a = Math.abs(Number(a) || 0); b = Math.abs(Number(b) || 0);
  while (b) { const t = b; b = a % b; a = t; }
  return a || 1;
}

function originFromTarget(raw) {
  try {
    const u = new URL(raw || CHATGPT);
    if (u.hostname === "chatgpt.com" || u.hostname === "chat.openai.com") return u.origin;
  } catch (_) {}
  return CHATGPT;
}

async function waitTabComplete(tabId, timeoutMs = 45000) {
  const end = Date.now() + Math.max(1000, timeoutMs);
  while (Date.now() < end) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab && tab.status === "complete") return true;
    } catch (_) {}
    await sleep(250);
  }
  return false;
}

async function ensureGptTab(targetUrl = CHATGPT) {
  const origin = originFromTarget(targetUrl);
  const tabs = await chrome.tabs.query({});
  const exact = tabs.find(t => (t.url || "") === targetUrl);
  const found = exact || tabs.find(t => (t.url || "").startsWith(origin + "/")) || tabs.find(t => (t.url || "").startsWith("https://chatgpt.com/"));
  if (found && found.id) {
    if (targetUrl && !(found.url || "").startsWith(origin + "/")) {
      await chrome.tabs.update(found.id, { url: targetUrl, active: true });
      await waitTabComplete(found.id, 45000);
      await sleep(800);
    } else {
      await chrome.tabs.update(found.id, { active: true });
    }
    return found.id;
  }
  const tab = await chrome.tabs.create({ url: targetUrl || CHATGPT, active: true });
  if (tab && tab.id) {
    await waitTabComplete(tab.id, 45000);
    await sleep(1200);
  }
  return tab.id;
}

async function pageFetch(tabId, url, { method = "GET", headers = {}, body = null } = {}) {
  const frames = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [url, { method, headers, body }],
    func: async (u, opts) => {
      const init = { method: opts.method || "GET", headers: opts.headers || {}, credentials: "include" };
      if (opts.body !== null && opts.body !== undefined) init.body = JSON.stringify(opts.body);
      const r = await fetch(u, init);
      const text = await r.text();
      const hdrs = {};
      try { for (const [k, v] of r.headers.entries()) hdrs[k] = v; } catch (_) {}
      let json = null; try { json = text ? JSON.parse(text) : null; } catch (_) {}
      return { status: r.status, headers: hdrs, text, json, url: r.url };
    }
  });
  const res = Array.isArray(frames) && frames[0] ? frames[0].result : null;
  if (!res) throw new Error(`GPT pageFetch returned empty result for ${url}`);
  return res;
}

function gptHeaders(token, path = "/", accept = "application/json") {
  const h = {
    "Authorization": `Bearer ${token}`,
    "Accept": accept,
    "Content-Type": "application/json",
    "Origin": CHATGPT,
    "Referer": CHATGPT + "/",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Priority": "u=1, i",
    "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "Sec-Ch-Ua-Arch": '"x86"',
    "Sec-Ch-Ua-Bitness": '"64"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Model": '""',
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "OAI-Language": "en-US",
    "oai-hlib": "true",
    "oai-nav-state": "1",
    "oai-chat-web-route": "ChMxMC4xMzEuMTAxLjIzNjozMDAwENjSRg==",
    "OAI-Client-Version": WEB_CLIENT_VERSION,
    "OAI-Client-Build-Number": WEB_BUILD_NUMBER,
    "OAI-Device-Id": uuid(),
    "OAI-Session-Id": uuid(),
    "X-OpenAI-Target-Path": path,
    "X-OpenAI-Target-Route": path
  };
  if (accept === "text/event-stream") h["X-Oai-Turn-Trace-Id"] = uuid();
  return h;
}

async function getAccessToken(tabId, targetUrl) {
  const origin = originFromTarget(targetUrl);
  const r = await pageFetch(tabId, `${origin}/api/auth/session`, { headers: { "Accept": "application/json" } });
  const j = r && r.json || {};
  const tok = j.accessToken || j.access_token || j.token;
  if (!tok) throw new Error(`GPT access token not found: ${JSON.stringify(j).slice(0, 240)}`);
  return { type: "gpt_access_token", access_token: tok, expires: j.expires || null, email: j.user && j.user.email || null, raw: j, source: "extension.auth_session" };
}

function utf8Bytes(s) {
  return new TextEncoder().encode(String(s || ""));
}

function bytesToBase64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

function jsonB64(v) {
  return bytesToBase64(utf8Bytes(JSON.stringify(v)));
}

function hexToBytes(s) {
  let h = String(s || "").trim();
  if (!h) return null;
  if (h.length % 2) h = "0" + h;
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) {
    const n = Number.parseInt(h.slice(i * 2, i * 2 + 2), 16);
    if (!Number.isFinite(n)) return null;
    out[i] = n;
  }
  return out;
}

function rotl64(x, n) {
  n = BigInt(n);
  if (n === 0n) return x & MASK64;
  return ((x << n) | (x >> (64n - n))) & MASK64;
}

function keccakF1600(a) {
  const R = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
  ];
  const RC = [
    0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an, 0x8000000080008000n,
    0x000000000000808bn, 0x0000000080000001n, 0x8000000080008081n, 0x8000000000008009n,
    0x000000000000008an, 0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
    0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n,
    0x8000000000008002n, 0x8000000000000080n, 0x000000000000800an, 0x800000008000000an,
    0x8000000080008081n, 0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n,
  ];
  for (let round = 0; round < 24; round++) {
    const c = new Array(5).fill(0n);
    const d = new Array(5).fill(0n);
    for (let x = 0; x < 5; x++) c[x] = a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20];
    for (let x = 0; x < 5; x++) d[x] = c[(x + 4) % 5] ^ rotl64(c[(x + 1) % 5], 1);
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) a[x + 5 * y] = (a[x + 5 * y] ^ d[x]) & MASK64;
    const b = new Array(25).fill(0n);
    for (let x = 0; x < 5; x++) {
      for (let y = 0; y < 5; y++) {
        b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl64(a[x + 5 * y], R[x][y]);
      }
    }
    for (let x = 0; x < 5; x++) {
      for (let y = 0; y < 5; y++) {
        a[x + 5 * y] = (b[x + 5 * y] ^ ((~b[((x + 1) % 5) + 5 * y]) & b[((x + 2) % 5) + 5 * y])) & MASK64;
      }
    }
    a[0] = (a[0] ^ RC[round]) & MASK64;
  }
}

function sha3_512(bytes) {
  const state = new Array(25).fill(0n);
  let offset = 0;
  while (offset + SHA3_RATE_512 <= bytes.length) {
    for (let i = 0; i < SHA3_RATE_512; i++) {
      state[Math.floor(i / 8)] ^= BigInt(bytes[offset + i]) << BigInt((i % 8) * 8);
    }
    keccakF1600(state);
    offset += SHA3_RATE_512;
  }
  const block = new Uint8Array(SHA3_RATE_512);
  block.set(bytes.subarray(offset));
  block[bytes.length - offset] ^= 0x06;
  block[SHA3_RATE_512 - 1] ^= 0x80;
  for (let i = 0; i < SHA3_RATE_512; i++) {
    state[Math.floor(i / 8)] ^= BigInt(block[i]) << BigInt((i % 8) * 8);
  }
  keccakF1600(state);
  const out = new Uint8Array(64);
  for (let i = 0; i < out.length; i++) out[i] = Number((state[Math.floor(i / 8)] >> BigInt((i % 8) * 8)) & 0xffn);
  return out;
}

function compareBytesLE(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return a.length === b.length ? 0 : (a.length < b.length ? -1 : 1);
}

function powGenerate(seed, difficulty, config) {
  const diffBytes = hexToBytes(difficulty);
  if (!diffBytes || !diffBytes.length) return { answer: jsonB64(seed), solved: false };
  const static1 = JSON.stringify(config.slice(0, 3)).replace(/\]$/, ",");
  const mid = JSON.stringify(config.slice(4, 9)).replace(/^\[/, "").replace(/\]$/, "");
  const static2 = "," + mid + ",";
  const static3 = "," + JSON.stringify(config.slice(10)).replace(/^\[/, "");
  const seedBytes = utf8Bytes(seed);
  for (let i = 0; i < 500000; i++) {
    const final = static1 + String(i) + static2 + String(i >> 1) + static3;
    const encoded = bytesToBase64(utf8Bytes(final));
    const msg = new Uint8Array(seedBytes.length + encoded.length);
    msg.set(seedBytes, 0);
    msg.set(utf8Bytes(encoded), seedBytes.length);
    const digest = sha3_512(msg);
    if (compareBytesLE(digest.subarray(0, diffBytes.length), diffBytes) <= 0) return { answer: encoded, solved: true };
  }
  return { answer: jsonB64(seed), solved: false };
}

function estTimestampString() {
  const d = new Date(Date.now() - 5 * 3600 * 1000);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const pad = (n) => String(n).padStart(2, "0");
  return `${days[d.getUTCDay()]} ${months[d.getUTCMonth()]} ${pad(d.getUTCDate())} ${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} GMT-0500 (Eastern Standard Time)`;
}

function powConfig(userAgent = WEB_USER_AGENT) {
  const nowMs = Date.now() + Math.random();
  return [
    3000 + Math.floor(Math.random() * 3) * 1000,
    estTimestampString(),
    4294705152,
    0,
    userAgent,
    "https://chatgpt.com/backend-api/sentinel/sdk.js",
    "",
    "en-US",
    "en-US,es-US,en,es",
    0,
    "webdriver≭false",
    "location",
    "window",
    nowMs,
    uuid(),
    "",
    16,
    Date.now() + Math.random(),
  ];
}

function buildLegacyRequirementsToken(userAgent = WEB_USER_AGENT) {
  const seed = Math.random().toFixed(16);
  const { answer } = powGenerate(seed, "0fffff", powConfig(userAgent));
  return "gAAAAAC" + answer;
}

function buildProofToken(seed, difficulty, userAgent = WEB_USER_AGENT) {
  const { answer, solved } = powGenerate(seed, difficulty, powConfig(userAgent));
  if (!solved) return "gAAAAAB" + jsonB64(seed);
  return "gAAAAAB" + answer;
}

async function requirements(tabId, token) {
  const path = "/backend-api/sentinel/chat-requirements";
  const r = await pageFetch(tabId, CHATGPT + path, {
    method: "POST",
    headers: gptHeaders(token, path),
    body: { p: buildLegacyRequirementsToken() }
  });
  if (!r || r.status >= 400) throw new Error(`chat-requirements failed HTTP ${r && r.status}: ${(r && r.text || "").slice(0, 500)}`);
  const j = r.json || {};
  if (j.arkose && j.arkose.required) throw new Error("GPT chat-requirements requires arkose");
  if (!j.token) throw new Error(`chat-requirements missing token: ${JSON.stringify(j).slice(0, 300)}`);
  const pow = j.proofofwork || j.proof_of_work || {};
  let proof = j.proof_token || "";
  if (!proof && pow && pow.required && pow.seed && pow.difficulty) proof = buildProofToken(pow.seed, pow.difficulty);
  return { token: j.token, proof_token: proof, so_token: j.so_token || "" };
}

function withRequirementHeaders(headers, reqs, conduit = "") {
  const h = { ...headers, "OpenAI-Sentinel-Chat-Requirements-Token": reqs.token || "" };
  if (reqs.proof_token) h["OpenAI-Sentinel-Proof-Token"] = reqs.proof_token;
  if (reqs.so_token) h["OpenAI-Sentinel-SO-Token"] = reqs.so_token;
  if (conduit) {
    h["X-Conduit-Token"] = conduit;
    h["OpenAI-Conduit-Token"] = conduit;
  }
  return h;
}

async function prepare(tabId, token, reqs, model) {
  const path = "/backend-api/f/conversation/prepare";
  const r = await pageFetch(tabId, CHATGPT + path, {
    method: "POST",
    headers: withRequirementHeaders(gptHeaders(token, path, "*/*"), reqs),
    body: {
      action: "next",
      fork_from_shared_post: false,
      parent_message_id: "client-created-root",
      model,
      client_prepare_state: "none",
      timezone: "Asia/Shanghai",
      timezone_offset_min: -480,
      conversation_mode: { kind: "primary_assistant" },
      system_hints: ["picture_v2"],
      attachment_mime_types: ["image/png", "image/jpeg", "image/webp"],
      supports_buffering: true,
      supported_encodings: ["v1"],
      client_contextual_info: { app_name: "chatgpt.com" },
      thinking_effort: "standard"
    }
  });
  if (!r || r.status >= 400) throw new Error(`conversation prepare failed HTTP ${r && r.status}: ${(r && r.text || "").slice(0, 500)}`);
  return (r.json || {}).conduit_token || "";
}

function dataUrlToBlob(dataUrl) {
  const [head, b64] = String(dataUrl || "").split(",", 2);
  const mime = ((head.match(/^data:([^;]+)/) || [])[1] || "application/octet-stream");
  const bin = atob(b64 || "");
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

async function downloadRefBlob(ref) {
  if (!ref) throw new Error("empty reference image");
  if (String(ref).startsWith("data:")) return dataUrlToBlob(ref);
  const r = await fetch(ref, { credentials: "omit" });
  if (!r.ok) throw new Error(`reference image download HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return await r.blob();
}

async function imageDimensions(blob) {
  try {
    if (typeof createImageBitmap === "function") {
      const bmp = await createImageBitmap(blob);
      const out = { width: bmp.width || 1024, height: bmp.height || 1024 };
      try { bmp.close(); } catch (_) {}
      return out;
    }
  } catch (_) {}
  return { width: 1024, height: 1024 };
}

async function processUploadStream(tabId, token, fileId, fileName) {
  const path = "/backend-api/files/process_upload_stream";
  const body = {
    file_id: fileId,
    use_case: "multimodal",
    index_for_retrieval: false,
    file_name: fileName,
    library_persistence_mode: "opportunistic",
    metadata: { store_in_library: true },
    entry_surface: "chat_composer"
  };
  const r = await pageFetch(tabId, CHATGPT + path, { method: "POST", headers: gptHeaders(token, path, "text/event-stream"), body });
  if (!r || r.status >= 400) throw new Error(`process upload HTTP ${r && r.status}: ${(r && r.text || "").slice(0, 300)}`);
  let libraryFileId = "";
  for (const line of String(r.text || "").split(/\r?\n/)) {
    const s = line.trim();
    if (!s || !s.startsWith("{")) continue;
    try {
      const j = JSON.parse(s);
      if (j.extra && j.extra.metadata_object_id) libraryFileId = j.extra.metadata_object_id;
    } catch (_) {}
  }
  return libraryFileId;
}

async function uploadImage(tabId, token, ref, index, runtime) {
  const blob = await downloadRefBlob(ref);
  const fileName = `image_${index + 1}.${(blob.type || "image/png").includes("jpeg") ? "jpg" : "png"}`;
  const path = "/backend-api/files";
  const dims = await imageDimensions(blob);
  await runtime.progress(8, { stage: "upload_reference", index: index + 1, size: blob.size, mime: blob.type || "application/octet-stream" });
  const meta = await pageFetch(tabId, CHATGPT + path, {
    method: "POST",
    headers: gptHeaders(token, path),
    body: { file_name: fileName, file_size: blob.size, use_case: "multimodal", width: dims.width, height: dims.height }
  });
  if (!meta || meta.status >= 400) throw new Error(`upload meta HTTP ${meta && meta.status}: ${(meta && meta.text || "").slice(0, 300)}`);
  const fileId = meta.json && meta.json.file_id;
  const uploadUrl = meta.json && meta.json.upload_url;
  if (!fileId || !uploadUrl) throw new Error(`upload meta missing file_id/upload_url: ${JSON.stringify(meta.json || {}).slice(0, 300)}`);
  const put = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": blob.type || "application/octet-stream", "x-ms-blob-type": "BlockBlob", "x-ms-version": "2020-04-08" },
    body: blob
  });
  if (!put.ok) throw new Error(`upload blob HTTP ${put.status}: ${(await put.text()).slice(0, 200)}`);
  const donePath = `/backend-api/files/${fileId}/uploaded`;
  const done = await pageFetch(tabId, CHATGPT + donePath, { method: "POST", headers: gptHeaders(token, donePath), body: {} });
  if (!done || done.status >= 400) throw new Error(`upload confirm HTTP ${done && done.status}: ${(done && done.text || "").slice(0, 300)}`);
  const libraryFileId = await processUploadStream(tabId, token, fileId, fileName);
  return { file_id: fileId, library_file_id: libraryFileId, file_name: fileName, file_size: blob.size, mime: blob.type || "image/png", width: dims.width, height: dims.height };
}

function messageContent(prompt, refs) {
  if (!refs || !refs.length) return { content: { content_type: "text", parts: [prompt] }, metadata: { system_hints: ["picture_v2"] } };
  const parts = [];
  const attachments = [];
  for (const ref of refs) {
    parts.push({ content_type: "image_asset_pointer", asset_pointer: "sediment://file_" + String(ref.file_id || "").replace(/^file_/, ""), width: ref.width || 1024, height: ref.height || 1024, size_bytes: ref.file_size || 0 });
    const att = { id: ref.file_id, mime_type: ref.mime, name: ref.file_name, size: ref.file_size, width: ref.width || 1024, height: ref.height || 1024, source: "library", is_big_paste: false };
    if (ref.library_file_id) att.library_file_id = ref.library_file_id;
    attachments.push(att);
  }
  parts.push(prompt);
  return {
    content: { content_type: "multimodal_text", parts },
    metadata: { system_hints: ["picture_v2"], attachments, developer_mode_connector_ids: [], selected_github_repos: [], selected_all_github_repos: false, serialization_metadata: { custom_symbol_offsets: [] } }
  };
}

function parseImage2Size(size) {
  const m = String(size || "").trim().match(/^(\d+)\s*x\s*(\d+)$/i);
  return m ? { width: Number(m[1]), height: Number(m[2]) } : null;
}

function isValidImage2Size(size) {
  const s = String(size || "").trim();
  if (!s) return false;
  if (s.toLowerCase() === "auto") return true;
  const dims = parseImage2Size(s);
  if (!dims) return false;
  const { width, height } = dims;
  if (!width || !height || width <= 0 || height <= 0) return false;
  if (width % 16 !== 0 || height % 16 !== 0) return false;
  if (Math.max(width, height) > GPT_IMAGE2_MAX_EDGE) return false;
  if (Math.max(width, height) / Math.min(width, height) > 3) return false;
  const pixels = width * height;
  return pixels >= GPT_IMAGE2_MIN_PIXELS && pixels <= GPT_IMAGE2_MAX_PIXELS;
}

function normalizeExplicitImage2Size(size) {
  const s = String(size || "").trim();
  if (!s) return "";
  if (s.toLowerCase() === "auto") return "auto";
  const dims = parseImage2Size(s);
  const out = dims ? `${dims.width}x${dims.height}` : s;
  return isValidImage2Size(out) ? out : "";
}

function image2TierFromSize(size) {
  const dims = parseImage2Size(size);
  if (!dims) return "";
  const pixels = dims.width * dims.height;
  if (pixels > 2400000 || Math.max(dims.width, dims.height) > 2048) return "4k";
  if (pixels > 1100000 || Math.max(dims.width, dims.height) > 1280) return "2k";
  return "1k";
}

function normalizeImage2Resolution(p) {
  const explicitSize = normalizeExplicitImage2Size(p && p.size);
  const tier = image2TierFromSize(explicitSize);
  if (tier) return tier;
  for (const k of ["resolution", "size_tier", "gpt_image2_resolution", "image_resolution"]) {
    const v = String((p && p[k]) || "").trim().toLowerCase().replace(/\s+/g, "");
    if (v === "1" || v === "1k" || v === "k1") return "1k";
    if (v === "2" || v === "2k" || v === "k2") return "2k";
    if (v === "4" || v === "4k" || v === "k4") return "4k";
  }
  const publicModel = String((p && (p.gpt_image2_model || p.model)) || "").trim().toLowerCase();
  if (GPT_IMAGE2_PUBLIC_MODELS[publicModel]) return GPT_IMAGE2_PUBLIC_MODELS[publicModel];
  return "1k";
}

function image2RatioFromSize(size) {
  const s = String(size || "").trim();
  if (!s || s.toLowerCase() === "auto") return "";
  for (const byRatio of Object.values(GPT_IMAGE2_SIZE_TABLE)) {
    for (const [ratio, candidate] of Object.entries(byRatio)) {
      if (candidate === s) return ratio;
    }
  }
  const dims = parseImage2Size(s);
  if (!dims) return "";
  const g = gcd(dims.width, dims.height);
  return `${dims.width / g}:${dims.height / g}`;
}

function normalizeImage2Ratio(p) {
  return String((p && (p.ratio || p.aspect_ratio || p.size_ratio || p.aspectRatio)) || "").trim() || image2RatioFromSize(p && p.size) || "1:1";
}

function normalizeImage2Size(p, resolution = "") {
  const explicit = normalizeExplicitImage2Size(p && p.size);
  if (explicit) return explicit;
  const tier = resolution || normalizeImage2Resolution(p);
  const ratio = normalizeImage2Ratio(p);
  const byRatio = GPT_IMAGE2_SIZE_TABLE[tier] || GPT_IMAGE2_SIZE_TABLE["1k"];
  return byRatio[ratio] || byRatio["1:1"] || "1024x1024";
}

function parseSize(size) {
  const m = String(size || "").trim().match(/^(\d+)x(\d+)$/i);
  return { width: m ? Number(m[1]) : 1024, height: m ? Number(m[2]) : 1024 };
}

function isImage2Payload(p) {
  const vals = [p && p.gpt_image2_model, p && p.model, p && p.model_code, p && p.image_model_name, p && p.upstream_model];
  return vals.some(v => {
    const s = String(v || "").trim().toLowerCase();
    return !!(GPT_IMAGE2_PUBLIC_MODELS[s] || s === "gpt-image-2" || s === "gpt-image2");
  });
}

function normalizeImage2Payload(p) {
  const out = { ...(p || {}) };
  const resolution = normalizeImage2Resolution(out);
  const size = normalizeImage2Size(out, resolution);
  let publicModel = String(out.gpt_image2_model || out.model || "").trim().toLowerCase();
  if (!GPT_IMAGE2_PUBLIC_MODELS[publicModel]) publicModel = `gpt-image2-${resolution}`;
  out.workflow_kind = "image";
  out.gpt_image2_model = publicModel;
  out.model_code = "gpt-image-2";
  out.image_model_name = "gpt-image-2";
  out.resolution = resolution;
  out.size_tier = resolution.toUpperCase();
  out.size = size;
  return out;
}

function normalizeWebModel(p, kind) {
  const explicit = p.web_model || p.chatgpt_model || p.model_slug;
  if (explicit) return explicit;
  if (isImage2Payload(p)) return "gpt-5-5-thinking";
  return p.model || p.model_code || (kind === "image" ? "gpt-5-5-thinking" : "gpt-5-5-thinking");
}

function appendRatioHint(prompt, p) {
  const ratio = p.ratio || p.aspect_ratio || p.size_ratio || "";
  let out = prompt;
  if (ratio && ratio !== "1:1") out = `${out}\n\n将宽高比设为 ${ratio}`;
  if (isImage2Payload(p)) {
    const resolution = normalizeImage2Resolution(p).toUpperCase();
    const size = normalizeImage2Size(p, resolution.toLowerCase());
    out = `${out}\n\n使用 gpt-image-2 生成 ${resolution} 图片，输出尺寸为 ${size}。`;
  }
  return out;
}

async function startConversation(tabId, token, reqs, conduit, payload, kind, refs) {
  const model = normalizeWebModel(payload, kind);
  const prompt = kind === "image" ? appendRatioHint(payload.prompt || "", payload) : (payload.prompt || "");
  const mc = messageContent(prompt, refs);
  const path = "/backend-api/f/conversation";
  const body = {
    action: "next",
    fork_from_shared_post: false,
    parent_message_id: "client-created-root",
    model,
    client_prepare_state: "success",
    timezone: "Asia/Shanghai",
    timezone_offset_min: -480,
    conversation_mode: { kind: "primary_assistant" },
    enable_message_followups: true,
    system_hints: [],
    supports_buffering: true,
    supported_encodings: ["v1"],
    client_contextual_info: { is_dark_mode: false, time_since_loaded: 51, page_height: 1111, page_width: 1731, pixel_ratio: 1.5, screen_height: 1440, screen_width: 2560, app_name: "chatgpt.com" },
    paragen_cot_summary_display_override: "allow",
    force_parallel_switch: "auto",
    thinking_effort: "standard",
    messages: [{ id: uuid(), author: { role: "user" }, create_time: Math.floor(Date.now()/1000), content: mc.content, metadata: mc.metadata }]
  };
  const headers = withRequirementHeaders(gptHeaders(token, path, "text/event-stream"), reqs, conduit);
  const r = await pageFetch(tabId, CHATGPT + path, { method: "POST", headers, body });
  if (!r || r.status >= 400) throw new Error(`conversation failed HTTP ${r && r.status}: ${(r && r.text || "").slice(0, 600)}`);
  const got = extractAssets(r.text);
  return { ...got, raw_text_tail: String(r.text || "").slice(-2000) };
}

function codexHeaders(token, path) {
  const h = gptHeaders(token, path, "text/event-stream");
  h["Originator"] = "codex-tui";
  h["OpenAI-Beta"] = "responses=v1";
  return h;
}

function image2MainModel(p) {
  const v = String((p && (p.main_model || p.codex_main_model || p.responses_model)) || "").trim();
  return v || "gpt-5.5";
}

function image2Quality(p) {
  const q = String((p && p.quality) || "").trim().toLowerCase();
  if (q === "draft" || q === "low") return "low";
  if (q === "standard" || q === "medium") return "medium";
  if (q === "hd" || q === "high") return "high";
  return "";
}

function image2Body(payload, prompt, refs, size) {
  const content = [{ type: "input_text", text: prompt || "生成一张高质量图片" }];
  for (const ref of refs || []) {
    if (ref) content.push({ type: "input_image", image_url: ref });
  }
  const action = (refs && refs.length) || String(payload.operation || "").toLowerCase() === "edit" ? "edit" : "generate";
  const tool = { type: "image_generation", action, model: "gpt-image-2", size };
  const quality = image2Quality(payload);
  if (quality) tool.quality = quality;
  for (const k of ["background", "output_format", "output_compression", "partial_images", "moderation", "input_fidelity"]) {
    if (payload[k] !== undefined && payload[k] !== null && payload[k] !== "") tool[k] = payload[k];
  }
  const mask = payload.mask || payload.mask_image_url;
  if (mask) tool.input_image_mask = { image_url: mask };
  return {
    instructions: "You are an image generation assistant. Follow the user's prompt and return the generated image.",
    stream: true,
    reasoning: { effort: "medium", summary: "auto" },
    parallel_tool_calls: true,
    include: ["reasoning.encrypted_content"],
    model: image2MainModel(payload),
    store: false,
    tool_choice: "auto",
    input: [{ type: "message", role: "user", content }],
    tools: [tool]
  };
}

function shouldRetryImage2WithoutToolChoice(text) {
  const s = String(text || "").toLowerCase();
  return s.includes("tool choice") && s.includes("image_generation") && s.includes("not found") && s.includes("tools");
}

async function runImage2Workflow(tabId, token, payload, runtime) {
  const p = normalizeImage2Payload(payload);
  const resolution = normalizeImage2Resolution(p);
  const size = normalizeImage2Size(p, resolution);
  const dims = parseSize(size);
  const count = Math.max(1, Math.min(Number(p.count || p.n || 1) || 1, 4));
  const refUrls = collectRefUrls(p);
  const urls = [];
  let lastTail = "";
  const path = "/backend-api/codex/responses";
  await runtime.progress(5, { stage: "image2_prepare", model: p.gpt_image2_model, resolution, size, count, ref_count: refUrls.length });
  for (let i = 0; i < count; i++) {
    let body = image2Body(p, appendRatioHint(p.prompt || "", p), refUrls, size);
    await runtime.progress(10 + Math.floor((i / count) * 70), { stage: "image2_submit", index: i + 1, count, resolution, size });
    let r = await pageFetch(tabId, CHATGPT + path, { method: "POST", headers: codexHeaders(token, path), body });
    if (r && r.status >= 400 && shouldRetryImage2WithoutToolChoice(r.text)) {
      body = { ...body };
      delete body.tool_choice;
      await runtime.progress(12 + Math.floor((i / count) * 70), { stage: "image2_retry_without_tool_choice", index: i + 1, count, resolution, size });
      r = await pageFetch(tabId, CHATGPT + path, { method: "POST", headers: codexHeaders(token, path), body });
    }
    if (!r || r.status >= 400) throw new Error(`gpt-image-2 ${resolution} failed HTTP ${r && r.status}: ${(r && r.text || "").slice(0, 600)}`);
    lastTail = String(r.text || "").slice(-2000);
    const got = extractAssets(r.text);
    urls.push(...(got.urls || []));
    if (uniq(urls).length >= count) break;
  }
  const outUrls = uniq(urls);
  if (!outUrls.length) throw new Error(`gpt-image-2 ${resolution} returned no images; tail=${lastTail.slice(0, 400)}`);
  await runtime.progress(100, { stage: "done", asset_count: outUrls.length, resolution, size });
  return {
    type: "gpt_workflow_image",
    workflow_kind: "image",
    model: p.gpt_image2_model,
    model_code: "gpt-image-2",
    resolution,
    size,
    width: dims.width,
    height: dims.height,
    urls: outUrls,
    share_url: outUrls[0] || "",
    image_url: outUrls[0] || "",
    raw_text_tail: lastTail
  };
}

function isAssetURL(u) {
  if (/^data:(image|video)\//i.test(String(u || ""))) return true;
  try {
    const x = new URL(u);
    const h = x.hostname.toLowerCase();
    const p = x.pathname.toLowerCase();
    if (p.includes("/$web/chatgpt/") || p.includes("filled-plus-icon") || p.includes("logo")) return false;
    return h.includes("files.oaiusercontent.com") || h.includes("oaidalleapiprodscus.blob.core.windows.net") || (h.endsWith(".blob.core.windows.net") && !p.includes("/$web/")) || /\.(mp4|webm|png|jpg|jpeg|webp)(\?|$)/i.test(u);
  } catch (_) {
    return false;
  }
}

function mimeForImageFormat(format) {
  switch (String(format || "").trim().toLowerCase()) {
    case "jpeg":
    case "jpg":
      return "image/jpeg";
    case "webp":
      return "image/webp";
    default:
      return "image/png";
  }
}

function addGeneratedURL(out, raw, outputFormat = "") {
  const u = String(raw || "").trim().replace(/\\\//g, "/").replace(/\\u0026/g, "&");
  if (!u) return;
  if (/^https?:\/\//i.test(u)) {
    if (isAssetURL(u) && !out.includes(u)) out.push(u);
    return;
  }
  if (/^data:(image|video)\//i.test(u)) {
    if (!out.includes(u)) out.push(u);
    return;
  }
  // response.output_item.done / response.completed 可能直接返回裸 base64。
  if (/^[A-Za-z0-9+/=\r\n]+$/.test(u) && u.length > 256) {
    const dataUrl = `data:${mimeForImageFormat(outputFormat)};base64,${u.replace(/\s+/g, "")}`;
    if (!out.includes(dataUrl)) out.push(dataUrl);
  }
}

function collectGeneratedPayload(v, out) {
  if (!v) return;
  if (Array.isArray(v)) {
    for (const item of v) collectGeneratedPayload(item, out);
    return;
  }
  if (typeof v !== "object") return;
  const outputFormat = v.output_format || v.format || "";
  addGeneratedURL(out, v.url, outputFormat);
  addGeneratedURL(out, v.download_url, outputFormat);
  addGeneratedURL(out, v.result, outputFormat);
  addGeneratedURL(out, v.b64_json, outputFormat);
  addGeneratedURL(out, v.image_b64, outputFormat);
  addGeneratedURL(out, v.partial_image_b64, outputFormat);
  for (const value of Object.values(v)) collectGeneratedPayload(value, out);
}

function parseSseJsonObjects(text) {
  const objects = [];
  let dataLines = [];
  const flush = () => {
    if (!dataLines.length) return;
    const data = dataLines.join("\n").trim();
    dataLines = [];
    if (!data || data === "[DONE]") return;
    try { objects.push(JSON.parse(data)); } catch (_) {}
  };
  for (const line of String(text || "").split(/\r?\n/)) {
    if (!line.trim()) {
      flush();
      continue;
    }
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  flush();
  return objects;
}

function extractAssets(text) {
  const s = String(text || "");
  const conversation_id = ((s.match(/"conversation_id"\s*:\s*"([^"]+)"/) || [])[1] || "");
  const file_ids = [];
  const sediment_ids = [];
  const urls = [];
  for (const m of s.matchAll(/file[-_][A-Za-z0-9][A-Za-z0-9_-]{7,}/g)) if (!file_ids.includes(m[0])) file_ids.push(m[0]);
  for (const m of s.matchAll(/sediment:\/\/([A-Za-z0-9_-]+)/g)) if (!sediment_ids.includes(m[1])) sediment_ids.push(m[1]);
  for (const m of s.matchAll(/https:\\?\/\\?\/[^\s"'\\]+/g)) {
    let u = m[0].replace(/\\\//g, "/").replace(/\\u0026/g, "&");
    if (isAssetURL(u) && !urls.includes(u)) urls.push(u);
  }
  for (const obj of parseSseJsonObjects(s)) collectGeneratedPayload(obj, urls);
  return { conversation_id, file_ids, sediment_ids, urls };
}

function filterGenerated(ids, refs) {
  const exclude = new Set();
  for (const r of refs || []) {
    if (r.file_id) exclude.add(r.file_id);
    if (r.library_file_id) exclude.add(r.library_file_id);
  }
  return uniq(ids).filter(x => !exclude.has(x));
}

async function downloadURL(tabId, token, path) {
  const r = await pageFetch(tabId, CHATGPT + path, { headers: gptHeaders(token, path) });
  if (!r || r.status >= 400) return "";
  return (r.json && (r.json.download_url || r.json.url)) || "";
}

async function resolveURLs(tabId, token, cid, fileIds, sedimentIds, refs) {
  const out = [];
  const files = filterGenerated(fileIds, refs);
  const seds = filterGenerated(sedimentIds, refs);
  for (const id of files) {
    if (!id || id === "file_upload") continue;
    let path = `/backend-api/files/download/${encodeURIComponent(id)}`;
    if (cid) path += `?conversation_id=${encodeURIComponent(cid)}&inline=false`;
    const u = await downloadURL(tabId, token, path);
    if (u && !out.includes(u)) out.push(u);
  }
  for (const id of seds) {
    if (!cid || !id) continue;
    const u = await downloadURL(tabId, token, `/backend-api/conversation/${encodeURIComponent(cid)}/attachment/${encodeURIComponent(id)}/download`);
    if (u && !out.includes(u)) out.push(u);
  }
  return out;
}

async function libraryAssetIDs(tabId, token, cid, refs, kind = "image") {
  if (!cid) return [];
  const path = "/backend-api/files/library";
  const r = await pageFetch(tabId, CHATGPT + path, {
    method: "POST",
    headers: gptHeaders(token, path),
    body: { limit: 20, cursor: null }
  });
  if (!r || r.status >= 400) return [];
  const items = (r.json && Array.isArray(r.json.items)) ? r.json.items : [];
  const ids = [];
  const wantVideo = String(kind || "").toLowerCase().includes("video");
  for (const item of items) {
    if (!item || !item.file_id || item.origination_thread_id !== cid) continue;
    if (item.state && String(item.state).toLowerCase() !== "ready") continue;
    const category = String(item.library_file_category || "").toLowerCase();
    const mime = String(item.mime_type || "").toLowerCase();
    if (wantVideo) {
      if (category && !category.includes("video") && !category.includes("image")) continue;
      if (mime && !mime.startsWith("video/") && !mime.startsWith("image/")) continue;
    } else {
      if (category && !category.includes("image")) continue;
      if (mime && !mime.startsWith("image/")) continue;
    }
    if (!ids.includes(item.file_id)) ids.push(item.file_id);
  }
  return filterGenerated(ids, refs);
}

async function pollConversation(tabId, token, cid, refs, maxWaitMs, intervalMs, runtime, kind = "image") {
  const end = Date.now() + maxWaitMs;
  let last = { conversation_id: cid, urls: [], file_ids: [], sediment_ids: [] };
  let attempt = 0;
  while (cid && Date.now() < end) {
    attempt++;
    const path = `/backend-api/conversation/${encodeURIComponent(cid)}`;
    const r = await pageFetch(tabId, CHATGPT + path, { headers: gptHeaders(token, path) });
    if (r && r.status < 400) {
      const got = extractAssets(r.text);
      last.file_ids = uniq([...(last.file_ids || []), ...(got.file_ids || [])]);
      last.sediment_ids = uniq([...(last.sediment_ids || []), ...(got.sediment_ids || [])]);
      last.urls = uniq([...(last.urls || []), ...(got.urls || [])]);
      if (attempt === 1 || attempt % 6 === 0) {
        try {
          const libIds = await libraryAssetIDs(tabId, token, cid, refs, kind);
          last.file_ids = uniq([...(last.file_ids || []), ...libIds]);
        } catch (_) {}
      }
      const resolved = await resolveURLs(tabId, token, cid, last.file_ids, last.sediment_ids, refs);
      last.urls = uniq([...(last.urls || []), ...resolved]);
      if (last.urls.length) return last;
    }
    const pct = 20 + Math.min(75, Math.floor((1 - (end - Date.now()) / maxWaitMs) * 75));
    await runtime.progress(pct, { stage: "poll", attempt, conversation_id: cid, urls: last.urls.length, file_ids: last.file_ids.length, sediment_ids: last.sediment_ids.length });
    await sleep(intervalMs);
  }
  return last;
}

function collectRefUrls(p) {
  const out = [];
  const add = (v) => {
    const s = typeof v === "string" ? v.trim() : (v && typeof v === "object" ? String(v.url || v.image_url || "").trim() : "");
    if (s && !out.includes(s)) out.push(s);
  };
  for (const k of ["image", "image_url", "imageUrl", "first_image_url", "firstImageUrl", "last_image_url", "lastImageUrl", "end_image_url", "endImageUrl", "mask_image_url", "mask", "reference_image"]) add(p[k]);
  for (const k of ["reference_urls", "reference_image_urls", "images", "image_urls", "imageUrls", "ref_assets", "reference_images", "input_images", "Ingredients_images", "ingredients_images"]) if (Array.isArray(p[k])) p[k].forEach(add);
  return out;
}

async function runConversationWorkflow(tabId, token, payload, kind, runtime) {
  await runtime.progress(5, { stage: "requirements", workflow_kind: kind });
  const reqs = await requirements(tabId, token);
  const refs = [];
  const refUrls = collectRefUrls(payload);
  for (let i = 0; i < refUrls.length; i++) refs.push(await uploadImage(tabId, token, refUrls[i], i, runtime));

  const count = Math.max(1, Math.min(Number(payload.count || payload.n || 1) || 1, 4));
  const maxWaitMs = Math.max(60, Number(payload.max_wait_seconds || payload.timeout_seconds || 600)) * 1000;
  const intervalMs = Math.max(500, Number(payload.poll_interval_seconds || 5) * 1000);
  const allUrls = [];
  const allFileIds = [];
  const allSedIds = [];
  const cids = [];
  let lastTail = "";

  for (let i = 0; i < count; i++) {
    const model = normalizeWebModel(payload, kind);
    const conduit = await prepare(tabId, token, reqs, model);
    await runtime.progress(15, { stage: "submit_conversation", index: i + 1, count });
    const started = await startConversation(tabId, token, reqs, conduit, payload, kind, refs);
    if (started.conversation_id) cids.push(started.conversation_id);
    lastTail = started.raw_text_tail || lastTail;
    let urls = uniq(started.urls || []);
    let fileIds = filterGenerated(started.file_ids || [], refs);
    let sedIds = filterGenerated(started.sediment_ids || [], refs);
    if (started.conversation_id && !urls.length) {
      const polled = await pollConversation(tabId, token, started.conversation_id, refs, maxWaitMs, intervalMs, runtime, kind);
      urls = uniq([...urls, ...(polled.urls || [])]);
      fileIds = uniq([...fileIds, ...(polled.file_ids || [])]);
      sedIds = uniq([...sedIds, ...(polled.sediment_ids || [])]);
    }
    allUrls.push(...urls);
    allFileIds.push(...fileIds);
    allSedIds.push(...sedIds);
    if (kind === "video" && urls.length) break;
  }

  const urls = uniq(allUrls);
  const file_ids = uniq(allFileIds);
  const sediment_ids = uniq(allSedIds);
  if (!urls.length) throw new Error(`GPT ${kind} returned no asset urls; conversation_id=${cids[0] || ""} file_ids=${file_ids.length} sediment_ids=${sediment_ids.length}`);
  await runtime.progress(100, { stage: "done", asset_count: urls.length, file_ids: file_ids.length, sediment_ids: sediment_ids.length });
  return {
    type: kind === "video" ? "gpt_workflow_video" : "gpt_workflow_image",
    workflow_kind: kind,
    conversation_id: cids[0] || "",
    conversation_ids: cids,
    urls,
    file_ids,
    sediment_ids,
    share_url: urls[0] || "",
    image_url: kind === "image" ? (urls[0] || "") : undefined,
    video_url: kind === "video" ? (urls[0] || "") : undefined,
    raw_text_tail: lastTail
  };
}

function isImageQuotaFeature(name) {
  const n = String(name || "").toLowerCase();
  return ["image_gen", "image_generation", "image_edit", "img_gen"].includes(n) || n.includes("image_gen") || n.includes("img_gen");
}

function normalizeBalance(data) {
  const d = data && typeof data === "object" ? data : {};
  let remaining = -1, total = -1, resetAt = 0;
  const limits = Array.isArray(d.limits_progress) ? d.limits_progress : [];
  for (const item of limits) {
    if (!item || !isImageQuotaFeature(item.feature_name)) continue;
    if (item.remaining != null && (remaining < 0 || Number(item.remaining) < remaining)) remaining = Number(item.remaining) || 0;
    const maxV = item.max_value ?? item.cap ?? item.total ?? item.limit;
    if (maxV != null && Number(maxV) > total) total = Number(maxV) || 0;
    if (total < 0 && item.remaining != null) {
      const used = item.used ?? item.used_value ?? item.consumed;
      if (used != null) total = (Number(item.remaining) || 0) + (Number(used) || 0);
    }
    if (item.reset_after) {
      const ts = Math.floor(Date.parse(item.reset_after) / 1000);
      if (Number.isFinite(ts) && ts > 0 && (!resetAt || ts < resetAt)) resetAt = ts;
    }
  }
  if (remaining < 0) remaining = Number(d.image_quota_remaining ?? d.remaining ?? d.remaining_quota ?? d.credits ?? 0) || 0;
  if (total < 0) total = Number(d.image_quota_total ?? d.quota_total ?? d.total ?? d.limit ?? 0) || 0;
  const plan = d.plan_type || d.account_plan || d.subscription || d.account_plan_type || null;
  return { remaining: Math.max(0, remaining), image_quota_remaining: Math.max(0, remaining), image_quota_total: Math.max(0, total), image_quota_reset_at: resetAt, membership: plan, plan_type: plan, default_model_slug: d.default_model_slug || null, blocked_features: Array.isArray(d.blocked_features) ? d.blocked_features : [], raw: d };
}

function jwtPayload(token) {
  try {
    const parts = String(token || "").split(".");
    if (parts.length < 2) return {};
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - parts[1].length % 4) % 4);
    const json = decodeURIComponent(Array.from(atob(b64)).map(c => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join(""));
    const obj = JSON.parse(json);
    return obj && typeof obj === "object" ? obj : {};
  } catch (_) {
    return {};
  }
}

function planFromToken(token) {
  const p = jwtPayload(token);
  const auth = p && p["https://api.openai.com/auth"];
  if (auth && typeof auth === "object") {
    for (const k of ["chatgpt_plan_type", "plan_type", "account_plan_type"]) {
      const v = String(auth[k] || "").trim();
      if (v) return v;
    }
  }
  for (const k of ["chatgpt_plan_type", "plan_type", "account_plan", "account_plan_type"]) {
    const v = String(p[k] || "").trim();
    if (v) return v;
  }
  return "";
}

async function refreshBalanceTask(tabId, token, runtime) {
  const path = "/backend-api/conversation/init";
  await runtime.progress(15, { stage: "conversation_init" });
  const h = gptHeaders(token, path);
  h["x-oai-is"] = "ois1";
  const r = await pageFetch(tabId, CHATGPT + path, {
    method: "POST",
    headers: h,
    body: {
      gizmo_id: null,
      requested_default_model: null,
      conversation_id: null,
      timezone_offset_min: -480,
      system_hints: ["picture_v2"]
    }
  });
  if (!r || r.status >= 400) throw new Error(`GPT balance conversation/init HTTP ${r && r.status}: ${(r && r.text || "").slice(0, 500)}`);
  const info = normalizeBalance(r.json || {});
  const jwtPlan = planFromToken(token);
  if (!info.membership && jwtPlan) {
    info.membership = jwtPlan;
    info.plan_type = jwtPlan;
  }
  await runtime.progress(100, { stage: "done", remaining: info.remaining, total: info.image_quota_total });
  return { type: "gpt_balance", ...info, source: "conversation_init" };
}
async function membershipTask(tabId, token, runtime) {
  const info = await refreshBalanceTask(tabId, token, runtime);
  const membership = String(info.membership || info.plan_type || planFromToken(token) || "").trim();
  return {
    type: "gpt_membership",
    membership: membership || null,
    plan_type: membership || null,
    plan_title: membership || null,
    subscription_end: null,
    default_model_slug: info.default_model_slug || null,
    blocked_features: info.blocked_features || [],
    raw: info.raw || null
  };
}

async function queryProgressTask(tabId, token, p, runtime) {
  const cid = p.conversation_id || p.conversationId || "";
  if (!cid) throw new Error("query_progress missing conversation_id");
  const kind = p.workflow_kind || "image";
  const got = await pollConversation(tabId, token, cid, [], Math.max(5, Number(p.max_wait_seconds || 20)) * 1000, Math.max(500, Number(p.poll_interval_seconds || 2) * 1000), runtime, kind);
  const file_ids = uniq([...(Array.isArray(p.file_ids) ? p.file_ids : []), ...(got.file_ids || [])]);
  const sediment_ids = uniq([...(Array.isArray(p.sediment_ids) ? p.sediment_ids : []), ...(got.sediment_ids || [])]);
  const resolved = await resolveURLs(tabId, token, cid, file_ids, sediment_ids, []);
  const urls = uniq([...(got.urls || []), ...resolved]);
  await runtime.progress(urls.length ? 100 : 60, { stage: urls.length ? "done" : "pending", conversation_id: cid, asset_count: urls.length });
  return { type: "gpt_progress", status: urls.length ? "completed" : "in_progress", workflow_kind: kind, conversation_id: cid, urls, image_url: kind === "image" ? (urls[0] || "") : undefined, video_url: kind === "video" ? (urls[0] || "") : undefined, file_ids, sediment_ids };
}

export async function runGptTask(msg, runtime) {
  const p = msg.payload || {};
  const target = p.target_url || p.gpt_url || CHATGPT;
  const tabId = await ensureGptTab(target);
  const action = String(p.action || p.workflow_kind || "").trim();

  if (action === "get_access_token" || action === "fetch_access_token" || action === "fetch_tokens") {
    return await getAccessToken(tabId, target);
  }

  const token = p.access_token || (await getAccessToken(tabId, target)).access_token;
  if (!token) throw new Error("GPT access_token missing");

  if (action === "refresh_balance" || action === "balance_refresh" || action === "get_balance" || action === "balance") {
    return await refreshBalanceTask(tabId, token, runtime);
  }
  if (action === "refresh_membership" || action === "membership_refresh" || action === "get_membership" || action === "membership" || action === "subscription_info") {
    return await membershipTask(tabId, token, runtime);
  }
  if (action === "query_progress" || action === "poll_task" || action === "get_task") {
    return await queryProgressTask(tabId, token, p, runtime);
  }

  const kind = (p.workflow_kind || "").toLowerCase().includes("video") ? "video" : "image";
  if (kind === "image" && isImage2Payload(p)) {
    try {
      return await runImage2Workflow(tabId, token, p, runtime);
    } catch (e) {
      const msg = String((e && e.message) || e || "");
      const canFallback = /HTTP\s+(401|403)\b|Unauthorized|Forbidden/i.test(msg) && p.image2_fallback !== false && p.direct_only !== true;
      if (!canFallback) throw e;
      await runtime.progress(12, { stage: "image2_fallback_conversation", reason: msg.slice(0, 220) });
      return await runConversationWorkflow(tabId, token, p, "image", runtime);
    }
  }
  return await runConversationWorkflow(tabId, token, p, kind, runtime);
}
