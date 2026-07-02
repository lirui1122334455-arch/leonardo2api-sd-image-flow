# Seedance 2 / Nana Banana / VEO 3.1 · NewAPI 视频接口说明

> 对外统一使用 NewAPI 风格异步接口：  
> **提交任务：`POST /v1/videos`**  
> **查询任务：`GET /v1/videos/{task_id}`**

## 1. 调用流程

### Step 1 · 提交任务

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3-1",
    "prompt": "an astronaut surfing on a glowing ocean at night, cinematic camera movement",
    "duration": 8,
    "aspect_ratio": "16:9"
  }'
```

提交成功后立即返回任务信息：

```json
{
  "id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "task_id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "object": "video",
  "created_at": 1714200000000,
  "status": "queued",
  "progress": 0,
  "model": "veo-3-1",
  "video_url": null,
  "metadata": {
    "result_urls": []
  },
  "seconds": "8",
  "duration": 8,
  "aspect_ratio": "16:9",
  "prompt": "an astronaut surfing on a glowing ocean at night, cinematic camera movement"
}
```

### Step 2 · 查询任务

```bash
curl https://xxx.xxx.xxx/v1/videos/8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234 \
  -H "Authorization: Bearer api-key"
```

处理中：

```json
{
  "id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "task_id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "object": "video",
  "created_at": 1714200000000,
  "status": "in_progress",
  "progress": 45,
  "model": "veo-3-1",
  "video_url": null,
  "metadata": {
    "result_urls": []
  }
}
```

视频完成：

```json
{
  "id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "task_id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "object": "video",
  "created_at": 1714200000000,
  "status": "completed",
  "progress": 100,
  "model": "veo-3-1",
  "video_url": "https://xxx.xxx.xxx/generated/video.mp4",
  "url": "https://xxx.xxx.xxx/generated/video.mp4",
  "metadata": {
    "result_urls": ["https://xxx.xxx.xxx/generated/video.mp4"]
  },
  "completed_at": 1714200180000
}
```

图片完成（`nana-banana-2` / `nana-banana-pro`）：

```json
{
  "id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "task_id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "object": "video",
  "created_at": 1714200000000,
  "status": "completed",
  "progress": 100,
  "model": "nana-banana-2",
  "video_url": null,
  "image_url": "https://xxx.xxx.xxx/generated/image.jpg",
  "url": "https://xxx.xxx.xxx/generated/image.jpg",
  "metadata": {
    "result_urls": ["https://xxx.xxx.xxx/generated/image.jpg"]
  },
  "completed_at": 1714200180000
}
```

失败：

```json
{
  "id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "task_id": "8c7e4b0e6f3b4b0a9e0a0d8f4f0c1234",
  "object": "video",
  "created_at": 1714200000000,
  "status": "failed",
  "progress": 0,
  "model": "veo-3-1",
  "video_url": null,
  "metadata": {
    "result_urls": []
  },
  "error": {
    "message": "任务失败原因",
    "code": "task_failed"
  }
}
```

| `status` | 含义 |
|---|---|
| `queued` | 排队中 |
| `in_progress` | 生成中，继续轮询 |
| `completed` | 已完成，取 `video_url` / `image_url` / `url` |
| `failed` | 失败，查看 `error.message` |

## 2. 支持模型

| 模型 | 输出 | 说明 |
|---|---|---|
| `seedance-2` | 视频 | 支持文生视频、首尾帧视频、多参考图视频 |
| `veo-3-1` | 视频 | 支持文生视频、首尾帧视频、多参考图视频 |
| `nana-banana-2` | 图片 | 支持文生图片、多参考图生成图片 |
| `nana-banana-pro` | 图片 | 支持文生图片、多参考图生成图片 |

## 3. 视频模型示例

### 3.1 Seedance 2 · 文生视频

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2",
    "prompt": "a golden retriever puppy running through sunflowers, cinematic, slow motion",
    "duration": 10,
    "aspect_ratio": "16:9",
    "resolution": "720p"
  }'
```

### 3.2 Seedance 2 · 首尾帧生成视频

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2",
    "prompt": "animate naturally from the first frame to the final frame",
    "duration": 10,
    "aspect_ratio": "9:16",
    "function_mode": "first_last_frames",
    "images": [
      "https://your-cdn.com/first.jpg",
      "https://your-cdn.com/last.jpg"
    ]
  }'
```

也可以使用：

```json
{
  "first_image_url": "https://your-cdn.com/first.jpg",
  "last_image_url": "https://your-cdn.com/last.jpg"
}
```

### 3.3 Seedance 2 · 多参考图生成视频

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2",
    "prompt": "use the character, outfit and product references to make a fashion commercial",
    "duration": 15,
    "aspect_ratio": "16:9",
    "function_mode": "omni_reference",
    "images": [
      "https://your-cdn.com/character.jpg",
      "https://your-cdn.com/outfit.jpg",
      "https://your-cdn.com/product.jpg"
    ]
  }'
```

### 3.4 VEO 3.1 · 文生视频

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3-1",
    "prompt": "a cinematic drone shot over a futuristic city at sunrise",
    "duration": 8,
    "aspect_ratio": "16:9"
  }'
```

### 3.5 VEO 3.1 · 首尾帧生成视频

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3-1",
    "prompt": "animate smoothly from the first frame to the last frame",
    "duration": 8,
    "aspect_ratio": "9:16",
    "images": [
      "https://your-cdn.com/first.jpg",
      "https://your-cdn.com/last.jpg"
    ]
  }'
```

### 3.6 VEO 3.1 · 多参考图生成视频

> VEO 多参考图视频请使用 `Ingredients_images`，最多 3 张。

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3-1",
    "prompt": "combine these references into a cinematic product launch video",
    "duration": 8,
    "aspect_ratio": "16:9",
    "Ingredients_images": [
      "https://your-cdn.com/ref-1.jpg",
      "https://your-cdn.com/ref-2.jpg",
      "https://your-cdn.com/ref-3.jpg"
    ]
  }'
```

## 4. 图片模型示例

### 4.1 Nana Banana 2 · 文生图片

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nana-banana-2",
    "prompt": "a cute banana mascot wearing sunglasses, studio lighting, high detail",
    "aspect_ratio": "1:1",
    "resolution": "1k"
  }'
```

### 4.2 Nana Banana 2 · 多参考图生成图片

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nana-banana-2",
    "prompt": "make a poster using the character style and product reference",
    "aspect_ratio": "4:3",
    "resolution": "1k",
    "images": [
      "https://your-cdn.com/character.jpg",
      "https://your-cdn.com/product.jpg"
    ]
  }'
```

### 4.3 Nana Banana Pro · 文生图片

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nana-banana-pro",
    "prompt": "premium editorial product photo, luxury magazine style",
    "aspect_ratio": "16:9",
    "resolution": "2k"
  }'
```

### 4.4 Nana Banana Pro · 多参考图生成图片

```bash
curl -X POST https://xxx.xxx.xxx/v1/videos \
  -H "Authorization: Bearer api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nana-banana-pro",
    "prompt": "create a premium product campaign image using all references",
    "aspect_ratio": "16:9",
    "resolution": "2k",
    "images": [
      "https://your-cdn.com/product.jpg",
      "https://your-cdn.com/background-style.jpg"
    ]
  }'
```

## 5. 请求字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model` | string | ✅ | `seedance-2` / `veo-3-1` / `nana-banana-2` / `nana-banana-pro` |
| `prompt` | string | ✅ | 生成提示词 |
| `duration` | int | 视频必填 | `seedance-2` 支持 `10` / `15`；`veo-3-1` 固定传 `8` |
| `aspect_ratio` | string | ❌ | 视频常用 `16:9` / `9:16`；图片支持 `1:1` / `4:3` / `3:4` / `16:9` / `9:16` |
| `resolution` | string | ❌ | `seedance-2` 支持 `480p` / `720p` / `1080p`；图片支持 `1k` / `2k` |
| `images` | array | ❌ | 参考图 URL 数组。Seedance 多参考图最多 9 张；VEO 首尾帧最多 2 张；Nana Banana 图片最多 10 张 |
| `first_image_url` | string | ❌ | 首帧或单参考图 URL |
| `last_image_url` | string | ❌ | 尾帧 URL |
| `function_mode` | string | ❌ | `seedance-2` 可用：`first_last_frames` / `omni_reference` |
| `Ingredients_images` | array | ❌ | `veo-3-1` 多参考图视频使用，最多 3 张 |

## 6. 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `task_id` | string | 任务 ID |
| `object` | string | 固定为 `video` |
| `created_at` | int | 创建时间戳，毫秒 |
| `status` | string | `queued` / `in_progress` / `completed` / `failed` |
| `progress` | int | 0–100 |
| `model` | string | 请求模型 |
| `video_url` | string\|null | 视频完成后返回视频地址 |
| `image_url` | string\|null | 图片模型完成后返回图片地址 |
| `url` | string\|null | 最终结果地址，视频/图片通用 |
| `metadata.result_urls` | array | 最终结果地址数组 |
| `error.message` | string | 失败时的错误信息 |
