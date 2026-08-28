# Flow Omni 1.1 Flash 透传 API

本文档用于下游电脑、前端或中转服务通过 `fpbrowser2api` 调用 Flow
Omni 1.1 Flash。下游只需要调用 HTTP API，不需要读取或传递 Flow 账号
Cookie、Access Token 或指纹浏览器配置。

## 1. 接口地址

将下面的 `BASE_URL` 替换为实际的 fpbrowser2api 地址：

```text
BASE_URL=http://SERVER_IP:PORT
```

| 用途 | 方法 | 路径 |
|---|---|---|
| 提交生成任务 | `POST` | `/v1/videos` |
| 查询任务 | `GET` | `/v1/videos/{task_id}` |
| 兼容查询任务 | `GET` | `/v1/tasks/{task_id}` |
| 查询模型 | `GET` | `/v1/models` |

请求头：

```http
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
```

## 2. 模型名称

推荐使用：

```text
omni-1.1-flash
```

兼容别名：

```text
gemini-omni-1.1-flash
gemini-omni-flash
gemini-omni
VEOomni
veo-omni
omni-flash
```

## 3. 公共参数

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model` | string | 是 | 推荐传 `omni-1.1-flash` |
| `prompt` | string | 是 | 生成提示词 |
| `duration` | integer | 否 | `4` / `6` / `8` / `10`，默认 `8` |
| `aspect_ratio` | string | 否 | `16:9` / `9:16`，默认 `16:9` |
| `resolution` | string | 否 | `360p` / `720p`，默认 `720p` |
| `video_mode` | string | 否 | 可显式传 `i2v` 或 `r2v` |
| `first_image_url` | string | 否 | 首帧图 URL |
| `last_image_url` | string | 否 | 尾帧图 URL，必须和首帧一起传 |
| `images` | array | 否 | 默认按 `[first, last]` 用于 I2V，最多 2 张 |
| `Ingredients_images` | array | 否 | Omni 素材参考图，最多 7 张 |

### 模式判定

1. 传 `Ingredients_images` 时走素材生视频（R2V）。
2. 传 `first_image_url` / `last_image_url` 或 `images` 时默认走图生视频（I2V）。
3. 仅传 `prompt` 时走文生视频（T2V）。
4. 需要把 `images` 当作素材参考图时，显式传 `video_mode: "r2v"`。

## 4. 文生视频

```json
{
  "model": "omni-1.1-flash",
  "prompt": "a cinematic product reveal with smooth camera movement",
  "duration": 8,
  "aspect_ratio": "16:9",
  "resolution": "720p"
}
```

## 5. 单首帧图生视频

```json
{
  "model": "omni-1.1-flash",
  "prompt": "animate the subject with natural motion",
  "duration": 6,
  "aspect_ratio": "9:16",
  "resolution": "720p",
  "first_image_url": "https://your-domain.com/first.jpg"
}
```

也可传：

```json
{
  "model": "omni-1.1-flash",
  "prompt": "animate the subject with natural motion",
  "duration": 6,
  "images": ["https://your-domain.com/first.jpg"]
}
```

## 6. 首尾帧图生视频

```json
{
  "model": "omni-1.1-flash",
  "prompt": "transition naturally from the opening frame to the ending frame",
  "duration": 8,
  "aspect_ratio": "16:9",
  "resolution": "360p",
  "first_image_url": "https://your-domain.com/first.jpg",
  "last_image_url": "https://your-domain.com/last.jpg"
}
```

`images` 写法：

```json
{
  "model": "omni-1.1-flash",
  "prompt": "transition naturally between the two frames",
  "duration": 8,
  "images": [
    "https://your-domain.com/first.jpg",
    "https://your-domain.com/last.jpg"
  ]
}
```

## 7. 素材生视频

```json
{
  "model": "omni-1.1-flash",
  "prompt": "combine these references into a polished product video",
  "duration": 10,
  "aspect_ratio": "16:9",
  "resolution": "720p",
  "Ingredients_images": [
    "https://your-domain.com/product.jpg",
    "https://your-domain.com/background.jpg",
    "https://your-domain.com/style.jpg"
  ]
}
```

使用 `images` 作为素材参考图：

```json
{
  "model": "omni-1.1-flash",
  "video_mode": "r2v",
  "prompt": "use all references to create a coherent video",
  "duration": 8,
  "images": [
    "https://your-domain.com/ref-1.jpg",
    "https://your-domain.com/ref-2.jpg",
    "https://your-domain.com/ref-3.jpg"
  ]
}
```

## 8. curl 示例

```bash
curl -X POST "${BASE_URL}/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omni-1.1-flash",
    "prompt": "animate the subject with subtle natural movement",
    "duration": 8,
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "first_image_url": "https://your-domain.com/first.jpg"
  }'
```

提交后返回 `task_id`，每 3-5 秒轮询：

```bash
curl "${BASE_URL}/v1/videos/TASK_ID" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## 9. 返回结果

提交成功：

```json
{
  "task_id": "TASK_ID",
  "status": "queued",
  "progress": 0,
  "model": "omni-1.1-flash"
}
```

生成完成：

```json
{
  "task_id": "TASK_ID",
  "status": "completed",
  "progress": 100,
  "video_url": "https://...",
  "url": "https://...",
  "metadata": {
    "result_urls": ["https://..."]
  }
}
```

生成失败：

```json
{
  "task_id": "TASK_ID",
  "status": "failed",
  "error": {
    "message": "error detail"
  }
}
```

## 10. 内部模型键

下游通常不需要直接传内部键；它们由 fpbrowser2api 根据请求自动选择。

| 模式 | 720p 模型键 |
|---|---|
| T2V | `abra_t2v_{4|6|8|10}s` |
| R2V | `abra_r2v_{4|6|8|10}s` |
| 单首帧 I2V | `abra_i2v_{4|6|8|10}s` |
| 首尾帧 I2V | `omni_flash_i2v_{4|6|8|10}s_first_last` |

360p 在对应模型键末尾增加 `_360p`。

## 11. 当前边界

- 当前透传已支持 T2V、单首帧 I2V、首尾帧 I2V 和素材 R2V。
- Flow 页面中的 Omni 视频编辑 `abra_edit`、音频参考和角色参考尚未开放为当前对外 API 字段。
- 不要在下游配置中填写 Flow Cookie、Google Access Token 或指纹浏览器窗口标识。
