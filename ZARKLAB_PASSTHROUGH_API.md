# Zark Lab 视频生成透传 API

更新时间：2026-08-21

本接口复用已经登录 Zark Lab 的指纹浏览器窗口生成视频。下游不需要提供
Zark Lab Token，只需要使用 fpbrowser2api 自己的 API Key。

## 1. 接口信息

- 本机 Base URL：`http://127.0.0.1:8000`
- 当前局域网 Base URL：`http://192.168.0.15:8000`
- 鉴权：`Authorization: Bearer <FPBROWSER2API_API_KEY>`
- 推荐创建接口：`POST /v1/videos`
- 推荐查询接口：`GET /v1/videos/{task_id}`
- 完整任务接口：`POST /v1/tasks`
- 完整查询接口：`GET /v1/tasks/{task_id}`
- 模型列表：`GET /v1/models`
- 内部任务类型：`zarklab_video`
- 调用方式：异步创建，轮询直到 `completed` 或 `failed`

下游通常使用 `/v1/videos` 即可。需要指定本地窗口映射时，使用
`/v1/tasks`，并把透传参数放在 `json` 对象中。

## 2. 模型列表

下游必须使用 Public Model ID，不要直接依赖 Provider Model ID。

| Public Model ID | Provider Model ID | 时长 | 清晰度 | 声音 |
|---|---|---:|---|---|
| `zark-seedance-2.5` | `fal-seedance-2-5` | 4-30 秒 | `480p`、`720p` | 可开关 |
| `zark-seedance-2.0-lite` | `fal-seedance-2-fast` | 4-15 秒 | `480p`、`720p` | 可开关 |
| `zark-seedance-2.0-mini` | `fal-seedance-2-mini` | 4-15 秒 | `480p`、`720p` | 可开关 |
| `zark-seedance-2.0` | `fal-seedance-2-pro` | 4-15 秒 | `480p`、`720p`、`1080p`、`4k` | 可开关 |
| `zark-minimax-h3` | `fal-minimax-h3` | 5-15 秒 | `768P`、`2K`、`4K` | 固定开启 |

查询公开模型：

```bash
curl "http://192.168.0.15:8000/v1/models" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

## 3. 通用请求参数

以下字段可以直接放在 `POST /v1/videos` 的 JSON 根对象中；使用
`POST /v1/tasks` 时放入根对象的 `json` 字段中。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `model` | string | 是 | - | 使用上表中的 Public Model ID |
| `prompt` | string | 是 | - | 视频提示词；不能为空 |
| `duration` | integer | 否 | 模型最短时长 | 必须在对应模型范围内 |
| `resolution` | string | 否 | 模型最低清晰度 | 必须是对应模型支持的值 |
| `aspect_ratio` | string | 否 | Seedance 为 `auto`；H3 为 `16:9` | 见比例表 |
| `sound` | string/boolean | 否 | `on` | `on`、`off`、`true`、`false`；H3 只能开启 |
| `reference_image_urls` | string[] | 否 | `[]` | 普通参考图片 URL |
| `reference_video_urls` | string[] | 否 | `[]` | 普通参考视频 URL |
| `reference_audio_urls` | string[] | 否 | `[]` | 普通参考音频 URL |
| `zark_file_ids` | string[] | 否 | `[]` | 已上传到当前 Zark 工作区的文件 ID |
| `first_image_url` | string | 否 | - | 首帧图片 URL；启用首帧/首尾帧模式 |
| `last_image_url` | string | 否 | - | 尾帧图片 URL；必须同时提供首帧 |
| `references` | object[] | 否 | `[]` | 已上传文件的结构化角色列表，见第 6 节 |
| `dry_run` | boolean | 否 | `false` | 只询价和检查余额，不提交生成、不扣生成费用 |
| `run_id` | string | 否 | 自动 UUID | Zark 运行追踪 ID；不是下游任务 ID |

### 画面比例

Seedance 2.5 和全部 Seedance 2.0 模型支持：

```text
auto, 16:9, 21:9, 4:3, 1:1, 3:4, 9:16
```

MiniMax H3 支持：

```text
16:9, 21:9, 4:3, 1:1, 3:4, 9:16
```

## 4. 参考素材限制

| 模型 | 图片 | 视频 | 音频 | 全部素材合计 |
|---|---:|---:|---:|---:|
| `zark-seedance-2.5` | 30 | 10 | 10 | 50 |
| `zark-seedance-2.0-lite` | 9 | 3 | 3 | 12 |
| `zark-seedance-2.0-mini` | 9 | 3 | 3 | 12 |
| `zark-seedance-2.0` | 9 | 3 | 3 | 12 |
| `zark-minimax-h3` | 9 | 3 | 3 | 12 |

规则：

1. 普通参考素材和首尾帧模式不能混用。
2. 尾帧不能单独使用，提供 `last_image_url` 时必须同时提供 `first_image_url`。
3. 只提供首帧时为图生视频。
4. 同时提供首帧和尾帧时，执行器使用 `interpolate` 动作。
5. 首帧和尾帧都计入图片数量和总素材数量。
6. `zark_file_ids` 计入总素材数量。
7. 相同 URL 或文件 ID 会去重。

URL 素材由本服务下载后上传到当前 Zark 工作区：

- 支持 `http://`、`https://` 和 `data:` URL。
- 建议生产环境只使用下游服务可长期访问的 HTTPS URL。
- 单个文件最大 `32 MB`。
- 响应 `Content-Type` 必须是 `image/*`、`video/*` 或 `audio/*`。
- URL 需要允许 fpbrowser2api 所在机器直接下载，不能依赖下游浏览器 Cookie。

## 5. 创建任务：推荐接口

### 5.1 纯文本生成

```bash
curl -X POST "http://192.168.0.15:8000/v1/videos" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zark-seedance-2.5",
    "prompt": "A cinematic tracking shot through a rainy neon street",
    "duration": 8,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "sound": "on"
  }'
```

创建成功：

```json
{
  "id": "TASK_ID",
  "task_id": "TASK_ID",
  "object": "video",
  "status": "queued",
  "progress": 0,
  "model": "zark-seedance-2.5",
  "video_url": null,
  "duration": 8,
  "seconds": "8",
  "aspect_ratio": "16:9",
  "metadata": {
    "result_urls": []
  }
}
```

### 5.2 多参考图、视频和音频

```bash
curl -X POST "http://192.168.0.15:8000/v1/videos" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zark-seedance-2.5",
    "prompt": "Keep the same subject, visual style, camera rhythm and sound atmosphere",
    "duration": 12,
    "resolution": "720p",
    "aspect_ratio": "9:16",
    "sound": "on",
    "reference_image_urls": [
      "https://cdn.example.com/character.png",
      "https://cdn.example.com/style.jpg"
    ],
    "reference_video_urls": [
      "https://cdn.example.com/motion.mp4"
    ],
    "reference_audio_urls": [
      "https://cdn.example.com/ambience.mp3"
    ]
  }'
```

### 5.3 首帧图生视频

```json
{
  "model": "zark-seedance-2.0",
  "prompt": "The camera slowly pushes forward while the subject turns toward the light",
  "duration": 8,
  "resolution": "1080p",
  "aspect_ratio": "16:9",
  "sound": "off",
  "first_image_url": "https://cdn.example.com/start.png"
}
```

### 5.4 首尾帧插值

```json
{
  "model": "zark-seedance-2.5",
  "prompt": "Create a smooth cinematic transition between the two frames",
  "duration": 10,
  "resolution": "720p",
  "aspect_ratio": "16:9",
  "sound": "on",
  "first_image_url": "https://cdn.example.com/start.png",
  "last_image_url": "https://cdn.example.com/end.png"
}
```

### 5.5 MiniMax H3

```json
{
  "model": "zark-minimax-h3",
  "prompt": "An epic wide shot of a spacecraft emerging from storm clouds",
  "duration": 10,
  "resolution": "2K",
  "aspect_ratio": "21:9",
  "sound": "on"
}
```

H3 的 `sound` 必须为 `on` 或 `true`。传 `off`、`false` 会导致任务失败。

## 6. 已上传文件 ID 和角色透传

已有 Zark 文件 ID 时，可以避免再次下载和上传。

普通参考素材：

```json
{
  "model": "zark-seedance-2.5",
  "prompt": "Use the attached references",
  "zark_file_ids": ["file-a", "file-b"]
}
```

需要明确媒体类型或首尾帧角色时，使用结构化 `references`：

```json
{
  "model": "zark-seedance-2.5",
  "prompt": "Create a smooth transition",
  "references": [
    {
      "file_id": "file-start",
      "role": "start_frame",
      "media_type": "image"
    },
    {
      "file_id": "file-end",
      "role": "end_frame",
      "media_type": "image"
    }
  ]
}
```

结构化字段：

| 字段 | 值 |
|---|---|
| `file_id` | 当前 Zark 工作区内的文件 ID |
| `role` | `inspiration`、`start_frame`、`end_frame` |
| `media_type` | `image`、`video`、`audio` |

`zark_file_ids` 和 `references[].file_id` 必须属于当前已登录账号使用的同一个
Zark 工作区。跨账号或跨工作区的 ID 可能无法读取。

## 7. 完整任务接口

需要固定使用某个本地映射或窗口时，调用 `POST /v1/tasks`：

```bash
curl -X POST "http://192.168.0.15:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "zarklab_video",
    "mapping_id": 58,
    "json": {
      "model": "zark-seedance-2.5",
      "prompt": "A cinematic tracking shot through a rainy neon street",
      "duration": 8,
      "resolution": "720p",
      "aspect_ratio": "16:9",
      "sound": "on"
    }
  }'
```

顶层字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_type_code` | string | 是 | 固定为 `zarklab_video` |
| `json` | object | 是 | Zark 透传参数 |
| `mapping_id` | integer | 否 | 固定任务类型映射；一般不传 |
| `window_pk` | integer | 否 | 固定本地窗口；一般不传 |

创建成功：

```json
{
  "success": true,
  "task_id": "TASK_ID"
}
```

生产下游建议不传 `mapping_id` 和 `window_pk`，由服务端调度器选择已配置窗口。

## 8. 查询任务

### 8.1 NewAPI/OpenAI 风格

```bash
curl "http://192.168.0.15:8000/v1/videos/TASK_ID" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

完成示例：

```json
{
  "id": "TASK_ID",
  "task_id": "TASK_ID",
  "object": "video",
  "status": "completed",
  "state": "completed",
  "task_status": "completed",
  "progress": 100,
  "model": "zark-seedance-2.5",
  "video_url": "https://zark-cdn.example.com/generated.mp4",
  "url": "https://zark-cdn.example.com/generated.mp4",
  "success": true,
  "final": true,
  "metadata": {
    "result_urls": [
      "https://zark-cdn.example.com/generated.mp4"
    ]
  }
}
```

### 8.2 完整任务结果

```bash
curl "http://192.168.0.15:8000/v1/tasks/TASK_ID" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

完成示例：

```json
{
  "task_id": "TASK_ID",
  "status": "completed",
  "progress": 100,
  "video_url": "https://zark-cdn.example.com/generated.mp4",
  "result_urls": [
    "https://zark-cdn.example.com/generated.mp4"
  ],
  "result": {
    "type": "zarklab_video",
    "provider": "zarklab",
    "workflow_kind": "video",
    "message": "Zark Lab video generation completed",
    "generation_id": "ZARK_RUN_ID",
    "run_id": "ZARK_RUN_ID",
    "file_ids": ["ZARK_FILE_ID"],
    "video_url": "https://zark-cdn.example.com/generated.mp4",
    "share_url": "https://zark-cdn.example.com/generated.mp4",
    "url": "https://zark-cdn.example.com/generated.mp4",
    "urls": [
      "https://zark-cdn.example.com/generated.mp4"
    ],
    "model": "zark-seedance-2.5",
    "provider_model": "fal-seedance-2-5",
    "duration": 8,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "sound": "on",
    "estimated_credits": 300,
    "remaining_quota": 24700,
    "elapsed_ms": 90000
  },
  "error_message": null,
  "content_violation": 0
}
```

任务状态：

```text
queued -> running -> completed
queued -> running -> failed
```

建议每 2-5 秒轮询一次。`completed` 和 `failed` 都是终态，不要继续轮询。

结果 URL 由 Zark 对象存储返回，可能是带有效期签名的地址。下游需要长期保存时，
应在任务完成后及时转存。

## 9. Dry Run 报价

`dry_run: true` 会执行账号、模型、参数和余额检查，但不会上传 URL 素材，也不会
提交真实生成。

```json
{
  "model": "zark-seedance-2.5",
  "prompt": "quote only",
  "duration": 8,
  "resolution": "720p",
  "aspect_ratio": "16:9",
  "sound": "on",
  "dry_run": true
}
```

任务最终结果：

```json
{
  "status": "completed",
  "progress": 100,
  "result": {
    "type": "zarklab_video_quote",
    "provider": "zarklab",
    "workflow_kind": "video",
    "dry_run": true,
    "allowed": true,
    "estimated_credits": 1117,
    "available_credits": 25000,
    "model": "zark-seedance-2.5",
    "provider_model": "fal-seedance-2-5",
    "duration": 8,
    "resolution": "720p",
    "aspect_ratio": "16:9"
  }
}
```

价格会随模型、时长、清晰度和平台计费规则变化，示例数字不能作为固定价格。

## 10. 兼容别名

建议下游只使用前文的规范字段。完整任务接口同时接受以下别名：

| 规范字段 | 接受的别名 |
|---|---|
| `model` | `zarklab_model`、`selected_model`、`provider_model` |
| `prompt` | `text`、`input` |
| `duration` | `seconds` |
| `aspect_ratio` | `ratio` |
| `sound` | `audio` |
| `dry_run` | `skip_submit` |
| `first_image_url` | `start_frame_url`、`first_frame_image_url`、`start_image_url` |
| `last_image_url` | `end_frame_url`、`last_frame_image_url`、`end_image_url` |
| `reference_image_urls` | `image`、`image_url`、`images`、`image_urls`、`reference_images` |
| `reference_video_urls` | `video`、`video_url`、`videos`、`video_urls`、`reference_videos` |
| `reference_audio_urls` | `audio_url`、`audios`、`audio_urls`、`reference_audios` |
| `zark_file_ids` | `current_attachment_file_ids`、`reference_file_ids`、`file_ids`、`attachment_file_ids` |

已上传文件 ID 还支持按媒体类型传递：

- `reference_image_file_ids` / `image_file_ids`
- `reference_video_file_ids` / `video_file_ids`
- `reference_audio_file_ids` / `audio_file_ids`
- `start_frame_file_id` / `first_frame_file_id` / `first_image_file_id`
- `end_frame_file_id` / `last_frame_file_id` / `last_image_file_id`

## 11. 错误处理

创建接口本身通常先返回异步任务 ID。模型参数、Zark 登录态或生成阶段发生的错误，
会在查询接口中体现为 `status: "failed"` 和 `error_message`。

常见错误：

| HTTP/任务状态 | 原因 | 处理方式 |
|---|---|---|
| `400` | 缺少 model/prompt、任务类型不存在或请求结构错误 | 修正请求，不要原样重试 |
| `401` | fpbrowser2api API Key 错误，或 Zark 登录态过期 | 区分创建接口与任务错误；重新登录 Zark 窗口 |
| `404` | task_id 不存在 | 检查任务 ID 和服务实例 |
| `429` | 创建任务并发过高 | 指数退避后重试 |
| `502` | Zark 请求、素材下载/上传或结果读取失败 | 检查 URL、窗口网络和 Zark 状态 |
| `503` | 服务维护中，暂不接收新任务 | 延迟后重试 |
| `failed` | 参数越界、素材超限、余额不足、登录失效或平台生成失败 | 读取 `error_message` 后分类处理 |

典型参数错误：

```text
payload.prompt cannot be empty
fal-seedance-2-5 duration must be between 4 and 30 seconds
fal-minimax-h3 always generates sound; sound must be on
Zark Lab frame mode cannot be combined with inspiration references
Zark Lab end frame requires a start frame
Zark Lab references must be at most N in total
Zark reference file must be at most 32 MB
```

失败响应示例：

```json
{
  "task_id": "TASK_ID",
  "status": "failed",
  "progress": 0,
  "result": null,
  "error_message": "Zark Lab end frame requires a start frame",
  "content_violation": 0
}
```

## 12. 下游接入建议

1. 模型 ID 固定使用 `zark-*` Public Model ID，避免与其他 Seedance 渠道重名。
2. 下游按第 2、3、4 节做前置参数校验，避免创建必然失败的异步任务。
3. 普通参考素材与首尾帧在 UI 和数据模型中做成互斥模式。
4. 默认使用 `/v1/videos`；只有运维调试才使用 `/v1/tasks` 的映射参数。
5. 创建成功后保存 `task_id`，按 2-5 秒间隔轮询。
6. 只把 `completed` 视为生成成功；`failed` 不应自动无限重试。
7. 对素材下载错误、429、临时 502 使用有限次数指数退避。
8. 对参数 400、余额不足、登录失效和内容拒绝不要自动原样重试。
9. 完成后及时转存结果 URL，并保留 `run_id`、`file_ids` 便于排查。
10. 生产调用前先用 `dry_run: true` 检查账号、额度和参数，不会提交生成。

## 13. 运行要求

- Zark 指纹窗口必须保持有效登录态。
- `zarklab_video` 必须绑定到已启用的 Zark 窗口映射。
- 下游机器必须能访问 fpbrowser2api Base URL。
- fpbrowser2api 机器必须能访问下游提供的素材 URL。
- 网页接口、平台模型或风控规则变化时，可能需要重新登录或更新执行器。

