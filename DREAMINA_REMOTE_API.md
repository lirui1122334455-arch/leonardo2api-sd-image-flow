# Dreamina 远程生成视频 API 文档

本文档给另一台远程电脑调用本机 `fpbrowser2api` 服务使用。远程电脑只需要访问服务地址、携带 API Key、创建任务并轮询任务结果。

## 1. 基本信息

把下面的 `BASE_URL` 换成你实际暴露给远程电脑的地址：

```text
BASE_URL=http://你的服务器IP:端口
API_KEY=你的API_KEY
```

请求头统一使用：

```http
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
```

推荐使用视频兼容接口：

```http
POST {BASE_URL}/v1/videos
GET  {BASE_URL}/v1/videos/{task_id}
```

也可以使用原生任务接口：

```http
POST {BASE_URL}/v1/tasks
GET  {BASE_URL}/v1/tasks/{task_id}
```

## 2. 创建 Dreamina 视频任务

### 2.1 文生视频

```bash
curl -X POST "{BASE_URL}/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2-fast",
    "prompt": "a cinematic product video, smooth camera movement, premium lighting",
    "duration": 15,
    "aspect_ratio": "16:9",
    "resolution": "720p"
  }'
```

成功后会立即返回 `task_id`：

```json
{
  "id": "94dbbbcffc2146c899d83a14a2230fa8",
  "task_id": "94dbbbcffc2146c899d83a14a2230fa8",
  "object": "video",
  "status": "queued",
  "progress": 0,
  "model": "seedance-2-fast",
  "video_url": null,
  "metadata": {
    "result_urls": []
  }
}
```

### 2.2 混合参考图、视频、音频

```bash
curl -X POST "{BASE_URL}/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2-fast",
    "prompt": "combine these references into a polished short video",
    "duration": 15,
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "function_mode": "omni_reference",
    "images": [
      "https://your-domain.com/ref1.jpg"
    ],
    "videos": [
      { "url": "https://your-domain.com/ref-video.mp4", "duration": 6 }
    ],
    "audios": [
      { "url": "https://your-domain.com/ref-audio.mp3", "duration": 4 }
    ]
  }'
```

### 2.3 首尾帧视频

首尾帧只支持图片，不支持同时传视频或音频。

```bash
curl -X POST "{BASE_URL}/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2",
    "prompt": "animate from the first frame to the last frame",
    "duration": 15,
    "aspect_ratio": "16:9",
    "resolution": "1080p",
    "function_mode": "first_last_frames",
    "images": [
      "https://your-domain.com/first.jpg",
      "https://your-domain.com/last.jpg"
    ]
  }'
```

## 3. 参数说明

| 参数 | 必填 | 说明 |
|---|---:|---|
| `model` | 是 | `seedance-2`、`seedance-2-fast`、`seedance-2-mini` |
| `prompt` | 是 | 视频提示词 |
| `duration` | 是 | Dreamina 支持 `4` 到 `15` 秒 |
| `aspect_ratio` | 否 | `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`21:9`，默认 `16:9` |
| `resolution` | 否 | Fast/Mini 使用 `720p`；Pro 支持 `720p`、`1080p`、`4k` |
| `function_mode` | 否 | `omni_reference` 或 `first_last_frames`，默认 `omni_reference` |
| `images` | 否 | 参考图片数组，最多 9 张 |
| `videos` | 否 | 参考视频数组，最多 3 个，总时长不超过 15 秒 |
| `audios` | 否 | 参考音频数组，最多 3 个，总时长不低于 2 秒且不超过 15 秒 |
| `dry_run` | 否 | `true` 时只走流程校验和素材上传，不提交生成，不消耗生成积分 |

模型对应关系：

```text
seedance-2       = Dreamina Seedance 2.0 Pro
seedance-2-fast  = Dreamina Seedance 2.0 Fast
seedance-2-mini  = Dreamina Seedance 2.0 Mini
```

积分预估：

```text
seedance-2-mini  = 31 * duration
seedance-2-fast  = 35 * duration
seedance-2       = 43 * duration
```

重要限制：

```text
1. 远程电脑不要传它本机的 C:\... 路径，服务器读不到。
2. 远程调用素材建议全部传 https://... URL。
3. 只有素材文件在运行 fpbrowser2api 的服务器本机上时，才可以传服务器本机路径。
4. first_last_frames 只支持最多 2 张图片，不能混传视频/音频。
```

## 4. 轮询任务结果

创建任务返回 `task_id` 后，每 3-5 秒轮询一次：

```bash
curl -X GET "{BASE_URL}/v1/videos/{task_id}" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

处理中示例：

```json
{
  "task_id": "94dbbbcffc2146c899d83a14a2230fa8",
  "status": "running",
  "state": "running",
  "task_status": "running",
  "progress": 42,
  "success": false,
  "final": false,
  "video_url": null,
  "metadata": {
    "result_urls": []
  }
}
```

成功示例：

```json
{
  "task_id": "94dbbbcffc2146c899d83a14a2230fa8",
  "status": "completed",
  "state": "completed",
  "task_status": "completed",
  "progress": 100,
  "success": true,
  "final": true,
  "video_url": "https://...",
  "url": "https://...",
  "metadata": {
    "result_urls": ["https://..."]
  }
}
```

失败示例：

```json
{
  "task_id": "94dbbbcffc2146c899d83a14a2230fa8",
  "status": "failed",
  "state": "failed",
  "task_status": "failed",
  "progress": 100,
  "success": false,
  "final": true,
  "error_message": "错误原因"
}
```

最终视频 URL 按优先级取：

```text
video_url
url
metadata.result_urls[0]
```

## 5. JavaScript 轮询示例

```js
async function createDreaminaVideo(baseUrl, apiKey, payload) {
  const r = await fetch(`${baseUrl}/v1/videos`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || data.error_message || "create task failed");
  return data.task_id || data.id;
}

async function pollDreaminaVideo(baseUrl, apiKey, taskId) {
  while (true) {
    const r = await fetch(`${baseUrl}/v1/videos/${taskId}`, {
      headers: { Authorization: `Bearer ${apiKey}` }
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || data.error_message || "poll task failed");

    if (data.status === "completed" || data.final === true && data.success === true) {
      return data.video_url || data.url || data.metadata?.result_urls?.[0];
    }

    if (data.status === "failed" || data.success === false && data.final === true) {
      throw new Error(data.error_message || data.error?.message || "task failed");
    }

    await new Promise(resolve => setTimeout(resolve, 4000));
  }
}

async function run() {
  const baseUrl = "http://你的服务器IP:端口";
  const apiKey = "YOUR_API_KEY";

  const taskId = await createDreaminaVideo(baseUrl, apiKey, {
    model: "seedance-2-fast",
    prompt: "a cinematic product video, smooth camera movement",
    duration: 15,
    aspect_ratio: "16:9",
    resolution: "720p"
  });

  const videoUrl = await pollDreaminaVideo(baseUrl, apiKey, taskId);
  console.log(videoUrl);
}
```

## 6. 原生 `/v1/tasks` 调用方式

如果远程电脑不走 `/v1/videos`，也可以直接指定任务类型：

```bash
curl -X POST "{BASE_URL}/v1/tasks" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "dreamina_workflow",
    "json": {
      "model": "seedance-2-fast",
      "prompt": "a cinematic product video",
      "duration": 15,
      "aspect_ratio": "16:9",
      "resolution": "720p"
    }
  }'
```

返回：

```json
{
  "success": true,
  "task_id": "94dbbbcffc2146c899d83a14a2230fa8"
}
```

轮询：

```http
GET {BASE_URL}/v1/tasks/{task_id}
Authorization: Bearer YOUR_API_KEY
```

## 7. 联调建议

第一次远程联调建议先加：

```json
{
  "dry_run": true
}
```

`dry_run=true` 会走参数校验和上传流程，但不会提交 Dreamina 生成接口，适合确认远程参数、素材 URL、窗口绑定是否正常。

确认返回正常后，再去掉 `dry_run` 发正式生成任务。
