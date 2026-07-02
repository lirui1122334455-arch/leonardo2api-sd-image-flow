import { ensureTab, fetchJson, compactErrorResponse } from "./common.js";

const API_US = "https://dreamina-api.us.capcut.com";
const GENERATE_PATH = "/mweb/v1/aigc_draft/generate";
const HISTORY_PATH = "/mweb/v1/get_history_by_ids";
const REMOVE_HISTORY_PATH = "/mweb/v1/remove_history";
const AID = 513641;
const APPVR = "8.4.0";
const WEB_VERSION = "7.5.0";
const DRAFT_VERSION = "3.3.17";
const FEATURES = "app_lip_sync";

async function md5Hex(s) {
  const data = new TextEncoder().encode(s);
  const digest = await crypto.subtle.digest("MD5", data);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function headersFor(uri, extra = {}) {
  const dt = Math.floor(Date.now() / 1000);
  const path = new URL(uri, "https://x").pathname;
  const sign = await md5Hex(`9e2c|${path.slice(-7)}|7|${APPVR}|${dt}||11ac`);
  return {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Lan": "en",
    "app-sdk-version": "48.0.0",
    "Appid": String(AID),
    "Appvr": APPVR,
    "Device-Time": String(dt),
    "Sign": sign,
    "Sign-Ver": "1",
    "Pf": "7",
    "loc": "US",
    "tdid": "",
    ...extra
  };
}

function buildGenerateBody(payload) {
  // 首版插件执行器：接收后端已规范化字段；复杂图片上传/subject 绑定后续迁移。
  const submitId = crypto.randomUUID();
  const model = payload.model_name || payload.model || "dreamina_seedance_40";
  const ratio = payload.aspect_ratio || "16:9";
  const duration = Number(payload.duration || 15);
  const prompt = payload.prompt || "";
  return {
    submitId,
    body: {
      submit_id: submitId,
      task_type: "video",
      aigc_type: "video",
      mode: "workbench",
      model,
      duration,
      aspect_ratio: ratio,
      prompt,
      input: { prompt, aspect_ratio: ratio, duration, model }
    }
  };
}

export async function runDreaminaTask(msg, runtime) {
  const p = msg.payload || {};
  const target = p.target_page || "https://dreamina.capcut.com/ai-tool/video/generate";
  await runtime.progress(2, { stage: "ensure_tab", url: target });
  await ensureTab("https://dreamina.capcut.com/", target);

  if ((p.image_refs || []).length || (p.prompt_subject_refs || []).length) {
    throw new Error("extension dreamina image/subject workflow scaffolded but not implemented yet; use legacy executor");
  }

  const apiBase = API_US;
  const { submitId, body } = buildGenerateBody(p);
  const generateUrl = `${apiBase}${GENERATE_PATH}?aid=${AID}&device_platform=web&region=US&da_version=${DRAFT_VERSION}&os=windows&web_component_open_flag=0&commerce_with_input_video=1&web_version=${WEB_VERSION}&aigc_features=${FEATURES}`;
  await runtime.progress(6, { stage: "submit_api" });
  const tx = await fetchJson(generateUrl, {
    method: "POST",
    headers: await headersFor(GENERATE_PATH, { Referer: target }),
    body
  });
  if (tx.status >= 400 || (tx.json && tx.json.ret && String(tx.json.ret) !== "0")) {
    throw new Error(`Dreamina submit failed: ${compactErrorResponse(tx)}`);
  }
  const aigc = tx.json?.data?.aigc_data || tx.json?.aigc_data || {};
  const taskId = aigc.history_record_id || aigc.task?.task_id;
  if (!taskId) throw new Error(`Dreamina submit missing task id: ${JSON.stringify(tx.json).slice(0, 500)}`);
  await runtime.progress(10, { stage: "submitted", task_id: taskId, submit_id: submitId });

  const deadline = Date.now() + Math.max(60000, Number(p.timeout_seconds || 600) * 1000);
  let videoUrl = "", thumbUrl = "", itemId = "";
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 5000));
    const histUrl = `${apiBase}${HISTORY_PATH}?aid=${AID}&web_version=${WEB_VERSION}&da_version=${DRAFT_VERSION}&aigc_features=${FEATURES}`;
    const hx = await fetchJson(histUrl, {
      method: "POST",
      headers: await headersFor(HISTORY_PATH, { Referer: target }),
      body: { history_ids: [taskId] }
    });
    const data = hx.json?.data || {};
    const item = (data.history_list || data.histories || [])[0] || {};
    itemId = item.item_id || item.id || "";
    const vv = item.video || item.video_info || item;
    videoUrl = vv.video_url || vv.play_url || vv.url || "";
    thumbUrl = vv.cover_url || vv.thumb_url || "";
    await runtime.progress(30, { stage: "polling", task_id: taskId });
    if (videoUrl) break;
  }
  if (!videoUrl) throw new Error("Dreamina polling timeout or missing video_url");

  try {
    const rmUrl = `${apiBase}${REMOVE_HISTORY_PATH}?aid=${AID}&web_version=${WEB_VERSION}&da_version=${DRAFT_VERSION}&aigc_features=${FEATURES}`;
    await fetchJson(rmUrl, { method: "POST", headers: await headersFor(REMOVE_HISTORY_PATH, { Referer: target }), body: { history_ids: [taskId] } });
  } catch (_) {}
  await runtime.progress(100, { stage: "done", task_id: taskId, video_url: videoUrl });
  return {
    type: "dreamina_workflow_video",
    message: "Dreamina 视频完成",
    share_url: videoUrl,
    thumb_url: thumbUrl,
    video_type: "t2v",
    workflow_kind: "video",
    task_id: taskId,
    history_id: taskId,
    submit_id: submitId,
    item_id: itemId
  };
}
