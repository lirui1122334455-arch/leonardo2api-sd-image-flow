# Flow Omni 1.1 Flash 下游透传规范

> 本文档是 Omni 1.1 Flash 的下游接入唯一规范。
> 下游必须先确定业务模式，再组装请求；不得仅根据图片数量猜测模式。

## 1. 接口

```text
POST /v1/videos
GET  /v1/videos/{task_id}
```

请求头：

```http
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
```

推荐模型名：

```text
omni-1.1-flash
```

支持的时长为 `4`、`6`、`8`、`10` 秒，分辨率为 `360p` 或 `720p`，
画面比例为 `16:9` 或 `9:16`。

## 2. 三种视频模式

| 业务模式 | `video_mode` | 图片字段 | 用途 |
|---|---|---|---|
| 文生视频 | 不传 | 不传图片字段 | 仅根据提示词生成 |
| 首帧/首尾帧图生视频 | `i2v` | `first_image_url`、可选 `last_image_url` | 固定开头画面，或固定开头和结尾画面 |
| 多图片素材参考 | `r2v` | `Ingredients_images` | 将人物、商品、环境或风格图片作为参考素材 |

关键规则：

1. 多图片参考必须显式传 `video_mode: "r2v"`。
2. 多图片参考只使用 `Ingredients_images`，数组顺序会原样保留。
3. 多图片参考不得转换成 `first_image_url` 和 `last_image_url`。
4. 首尾帧模式必须显式传 `video_mode: "i2v"`。
5. 下游不得因为图片正好有两张，就自动判断为首尾帧。
6. 同一个请求不要混用 `Ingredients_images`、`images`、`first_image_url` 和
   `last_image_url`。

## 3. 多图片参考 R2V

这是人物参考、商品参考、场景参考和风格参考应使用的模式。

### 正确请求

```json
{
  "model": "omni-1.1-flash",
  "video_mode": "r2v",
  "prompt": "@图1中的角色A与@图2中的角色B在拳击台上进行一场激烈对决，保持两人的外貌、发型和服装特征稳定一致",
  "duration": 8,
  "aspect_ratio": "16:9",
  "resolution": "720p",
  "Ingredients_images": [
    "https://your-domain.com/character-a.jpg",
    "https://your-domain.com/character-b.jpg"
  ]
}
```

Omni 1.1 Flash 最多支持 7 张素材参考图。推荐每张图片只表达一个明确主体：

- 角色参考图只保留一名角色，避免同一张图出现多个人物。
- 尽量去掉大段文字、分镜、环境拼图和无关素材。
- 角色、商品、环境、风格最好分别使用独立图片。
- 提示词中的 `@图1`、`@图2` 按 `Ingredients_images` 数组顺序对应。

### 错误请求：把参考图当成首尾帧

```json
{
  "model": "omni-1.1-flash",
  "video_mode": "i2v",
  "prompt": "让两名参考角色进行对决",
  "first_image_url": "https://your-domain.com/character-a.jpg",
  "last_image_url": "https://your-domain.com/character-b.jpg"
}
```

这个请求的含义是“从第一张画面过渡到第二张画面”，不是“参考两名角色”。

### 不推荐请求：使用通用 `images`

服务端兼容下面的写法：

```json
{
  "model": "omni-1.1-flash",
  "video_mode": "r2v",
  "prompt": "use all reference images",
  "images": [
    "https://your-domain.com/ref-1.jpg",
    "https://your-domain.com/ref-2.jpg"
  ]
}
```

但下游正式接入不要使用这种写法。`images` 是兼容字段，缺少明确模式时可能被
解释为 I2V。正式透传统一使用 `Ingredients_images`。

## 4. 单首帧 I2V

```json
{
  "model": "omni-1.1-flash",
  "video_mode": "i2v",
  "prompt": "保持首帧人物外貌，生成自然连贯的动作",
  "duration": 6,
  "aspect_ratio": "16:9",
  "resolution": "720p",
  "first_image_url": "https://your-domain.com/first-frame.jpg"
}
```

该模式使用内部模型键 `abra_i2v_{4|6|8|10}s`。

## 5. 首尾帧 I2V

```json
{
  "model": "omni-1.1-flash",
  "video_mode": "i2v",
  "prompt": "从首帧自然运动到尾帧，保持主体一致",
  "duration": 8,
  "aspect_ratio": "16:9",
  "resolution": "720p",
  "first_image_url": "https://your-domain.com/start.jpg",
  "last_image_url": "https://your-domain.com/end.jpg"
}
```

该模式使用内部模型键
`omni_flash_i2v_{4|6|8|10}s_first_last`。

## 6. 文生视频 T2V

```json
{
  "model": "omni-1.1-flash",
  "prompt": "a cinematic night city scene with smooth camera movement",
  "duration": 8,
  "aspect_ratio": "16:9",
  "resolution": "720p"
}
```

该模式不要传任何图片字段，使用内部模型键 `abra_t2v_{4|6|8|10}s`。

## 7. 下游映射规则

下游应保留独立的业务模式，不要只保留一个通用图片数组。

```text
if mode == "multi_reference":
    request.video_mode = "r2v"
    request.Ingredients_images = reference_image_urls
    不传 first_image_url
    不传 last_image_url
    不传 images

if mode == "first_frame":
    request.video_mode = "i2v"
    request.first_image_url = first_frame_url
    不传 Ingredients_images

if mode == "first_last_frames":
    request.video_mode = "i2v"
    request.first_image_url = first_frame_url
    request.last_image_url = last_frame_url
    不传 Ingredients_images

if mode == "text_to_video":
    不传 video_mode
    不传任何图片字段
```

下游发送前必须校验：

```text
r2v -> Ingredients_images 数量为 1-7，且不包含首尾帧字段
i2v -> first_image_url 必填，last_image_url 可选，且不包含 Ingredients_images
t2v -> 不包含任何图片字段
```

## 8. 提交与查询

提交示例：

```bash
curl -X POST "${BASE_URL}/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omni-1.1-flash",
    "video_mode": "r2v",
    "prompt": "use @图1 and @图2 as two independent character references",
    "duration": 8,
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "Ingredients_images": [
      "https://your-domain.com/character-a.jpg",
      "https://your-domain.com/character-b.jpg"
    ]
  }'
```

提交成功后会返回 `task_id`。查询任务：

```bash
curl "${BASE_URL}/v1/videos/TASK_ID" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## 9. 下游验收标准

多图片参考接入完成后，至少验证以下内容：

1. 两张参考图请求进入 R2V，而不是 I2V。
2. 请求体包含 `video_mode: "r2v"`。
3. 请求体包含 `Ingredients_images`，数组长度和顺序正确。
4. 请求体不包含 `first_image_url`、`last_image_url` 或 `images`。
5. 服务端选择的模型键为 `abra_r2v_{4|6|8|10}s`。
6. 日志显示 `ingredients=N images=0`。

如果日志显示 `ingredients=0 images=2`，说明下游仍把两张参考图当成了首尾帧，
必须修正下游映射，不能通过修改提示词解决。

## 10. 安全边界

- 下游只需要 API 地址和 API Key。
- 不要向下游传递 Flow Cookie、Google Access Token、项目 Token 或指纹浏览器配置。
- 不要在请求里指定指纹浏览器窗口或 Flow 账号；账号调度由服务端工作池处理。
