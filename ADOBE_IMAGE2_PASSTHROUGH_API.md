# Adobe Firefly GPT Image 2 透传 API

更新时间：2026-08-27

本接口复用已经登录 Adobe Firefly 的指纹浏览器窗口生成图片。下游不需要提供
Adobe Token、Cookie 或账号信息，只需要使用 fpbrowser2api 自己的 API Key。

当前服务可以绑定多个 Adobe 账号。下游请求不感知具体账号和窗口，服务会在已启用、
有剩余额度且并发可用的 Adobe 映射中自动调度。增加或更换 Adobe 账号时，下游协议
不需要修改。

## 0. 当前支持范围

| 能力 | 状态 | 说明 |
|---|---|---|
| 文生图 | 支持 | 不传参考图字段 |
| 图生图 | 支持 | 传 `image` 或 `images` 后自动切换 |
| 参考图数量 | 1-4 张 | 超过 4 张返回 `400`，重复地址自动去重 |
| 参考图来源 | URL / Data URL | 公开 HTTP(S) URL 或 `data:image/...;base64,...` |
| 输出质量 | low / medium / high | 推荐通过 Public Model ID 后缀指定 |
| 蒙版编辑 | 不支持 | 传蒙版字段会返回 `400` |
| 多账号池 | 支持 | 下游不指定账号、映射或窗口 |

图生图链路已用 `adobe-gpt-image2-low` 做过真实端到端验证，包括公共接口创建、
参考图上传、Adobe 提交、状态轮询、结果下载和额度刷新。

## 1. 接口约定

- Base URL：`http://127.0.0.1:8000`（跨机器调用时替换为实际服务地址）
- 鉴权：`Authorization: Bearer <FPBROWSER2API_API_KEY>`
- 推荐创建接口：`POST /v1/videos`
- 推荐查询接口：`GET /v1/videos/{task_id}`
- 完整任务接口：`POST /v1/tasks`
- 完整查询接口：`GET /v1/tasks/{task_id}`
- 模型列表：`GET /v1/models`
- 内部任务类型：`adobe_image2_workflow`
- 调用方式：异步创建，轮询到 `completed` 或 `failed`
- 回调方式：当前不提供 webhook/callback，下游必须轮询状态接口

`/v1/videos` 是项目现有的统一媒体兼容接口。虽然接口名是 `videos`，Adobe
GPT Image 2 的完成结果仍然是图片，并通过 `image_url` 返回。

## 2. 模型与供应商隔离

下游必须传 Public Model ID，不要传 Adobe 内部模型名 `gpt-image@2`。

| Public Model ID | 质量 | Adobe Provider Model |
|---|---|---|
| `adobe-gpt-image2` | `medium` | `gpt-image@2` |
| `adobe-gpt-image2-low` | `low` | `gpt-image@2` |
| `adobe-gpt-image2-medium` | `medium` | `gpt-image@2` |
| `adobe-gpt-image2-high` | `high` | `gpt-image@2` |

模型前缀用于严格区分供应商：

| 模型前缀 | 实际供应商 | 内部任务类型 |
|---|---|---|
| `adobe-gpt-image2-*` | Adobe Firefly | `adobe_image2_workflow` |
| `gpt-image2-*`、`gpt-image-2` | ChatGPT | `gpt_workflow` |

接入 Adobe 时必须使用 `adobe-gpt-image2-*`。原有 `gpt-image2-*` 的路由和功能
保持不变，不会被切换到 Adobe。

查询服务当前公开的模型：

```bash
curl "http://127.0.0.1:8000/v1/models" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

## 3. 请求参数

使用 `POST /v1/videos` 时，参数直接放在 JSON 根对象中；使用
`POST /v1/tasks` 时，参数放在 `json` 对象中。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `model` | string | 是 | - | 使用第 2 节中的 Public Model ID |
| `prompt` | string | 是 | - | 图片提示词；正式生成时不能为空 |
| `aspect_ratio` | string | 否 | `auto` | `auto` 或第 3.1 节列出的固定比例 |
| `quality` | string | 否 | 由模型决定 | `low`、`medium`、`high`；推荐直接用模型后缀选择 |
| `image` | string/object | 否 | - | 单张参考图；可传公开 HTTP(S) URL 或 `data:image/...;base64,...` |
| `images` | array | 否 | - | 多张参考图，最多 4 张；元素可为 URL 字符串或 `{ "url": "..." }` |
| `dry_run` | boolean | 否 | `false` | 只检查会话和额度，不点击生成、不消耗生成额度 |

注意：

1. 推荐通过 `model` 后缀选择质量，不要同时传入相互冲突的 `model` 和 `quality`。
2. Adobe 路由会把兼容字段 `duration` 固定为 `1`，下游可以不传。
3. 没有参考图时执行文生图；存在一张或多张参考图时自动执行图生图，不需要更换模型。
4. 单图兼容别名：`image_url`、`imageUrl`、`input_image`、`inputImage`、
   `input_image_url`、`inputImageUrl`、`reference_image`、`referenceImage`、
   `reference_image_url`、`referenceImageUrl`、`first_image_url`、`firstImageUrl`。
5. 多图兼容别名：`image_urls`、`imageUrls`、`input_images`、`inputImages`、
   `reference_images`、`referenceImages`、`reference_image_urls`、`referenceImageUrls`。
6. 重复地址按首次出现顺序去重。每张图最大 30 MB，支持 AVIF、GIF、JPEG、PNG、WebP；
   服务会校验 MIME、真实图片内容和尺寸。
7. 外部 URL 必须是公开 HTTP(S) 地址，不能指向内网。本地生成结果可直接使用
   `/public/adobe-image2-assets/...` 或 `/public/gpt-assets/...` 路径作为参考图。
8. 当前不支持蒙版；传入 `mask`、`mask_image_url` 等字段会返回参数错误。
9. 生成消耗 Adobe 账号额度；实际扣减以 Adobe 页面显示为准。

### 3.1 支持的图片比例

所有质量档位使用同一组比例：

| Adobe 网站名称 | `aspect_ratio` |
|---|---|
| Auto | `auto` |
| Cinematic banner | `8:1` |
| Panorama | `4:1` |
| Ultra Wide | `21:9` |
| Widescreen | `16:9` |
| Classic | `5:4` |
| Landscape | `4:3` |
| Wide | `3:2` |
| Square | `1:1` |
| Standard | `4:5` |
| Portrait | `3:4` |
| Tall | `2:3` |
| Vertical | `9:16` |
| Vertical banner | `1:4` |
| Vertical strip | `1:8` |

## 4. 推荐接法：`/v1/videos`

### 4.1 创建任务

```bash
curl -X POST "http://127.0.0.1:8000/v1/videos" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "adobe-gpt-image2-medium",
    "prompt": "A premium studio product photograph of a red ceramic vase on a clean white background",
    "aspect_ratio": "3:2"
  }'
```

创建成功示例：

```json
{
  "id": "TASK_ID",
  "task_id": "TASK_ID",
  "object": "video",
  "status": "queued",
  "progress": 0,
  "model": "adobe-gpt-image2-medium",
  "video_url": null,
  "duration": 1,
  "seconds": "1",
  "aspect_ratio": "3:2",
  "metadata": {
    "result_urls": []
  }
}
```

下游保存 `task_id`，不要等待创建请求直接返回图片。

### 4.2 图生图创建任务

传一张参考图：

```bash
curl -X POST "http://127.0.0.1:8000/v1/videos" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "adobe-gpt-image2-medium",
    "prompt": "Keep the product shape and material, replace the background with a premium studio setting",
    "aspect_ratio": "1:1",
    "image": "https://cdn.example.com/input/product.png"
  }'
```

传多张参考图：

```bash
curl -X POST "http://127.0.0.1:8000/v1/videos" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "adobe-gpt-image2-high",
    "prompt": "Use the subject from the first image and the lighting style from the second image",
    "aspect_ratio": "3:2",
    "images": [
      "https://cdn.example.com/input/subject.png",
      {"url": "https://cdn.example.com/input/style.jpg"}
    ]
  }'
```

也可以把 `image` 换成完整的 `data:image/png;base64,...`。JSON 中的 base64 会显著增大
请求体，生产环境更推荐使用可公开访问的对象存储 URL。

### 4.3 查询任务

```bash
curl "http://127.0.0.1:8000/v1/videos/TASK_ID" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

处理中示例：

```json
{
  "id": "TASK_ID",
  "task_id": "TASK_ID",
  "status": "processing",
  "state": "processing",
  "task_status": "processing",
  "progress": 45,
  "success": false,
  "final": false,
  "model": "adobe-gpt-image2-medium",
  "metadata": {
    "result_urls": []
  }
}
```

完成示例：

```json
{
  "id": "TASK_ID",
  "task_id": "TASK_ID",
  "status": "completed",
  "state": "completed",
  "task_status": "completed",
  "progress": 100,
  "success": true,
  "final": true,
  "model": "adobe-gpt-image2-medium",
  "image_url": "/public/adobe-image2-assets/TASK_ID-0.png",
  "url": "/public/adobe-image2-assets/TASK_ID-0.png",
  "video_url": null,
  "metadata": {
    "result_urls": [
      "/public/adobe-image2-assets/TASK_ID-0.png"
    ]
  }
}
```

为兼容不同版本，下游按以下优先级读取结果：

1. `image_url`
2. `url`
3. `metadata.result_urls[0]`

返回的是 fpbrowser2api 本地持久化后的公开图片地址，不需要携带 Adobe Cookie。
当前响应通常是以 `/public/...` 开头的相对路径；下游必须使用请求时的 Base URL
拼成绝对 URL，例如 `http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png`。
跨机器部署时，确保该地址可被下游访问。

### 4.4 失败结果

```json
{
  "id": "TASK_ID",
  "task_id": "TASK_ID",
  "status": "failed",
  "success": false,
  "final": true,
  "error": {
    "code": "task_failed",
    "message": "ERROR_MESSAGE"
  }
}
```

轮询终止条件：

- `status == "completed"`：读取图片 URL。
- `status == "failed"`：记录 `error.code` 和 `error.message`，不要无限轮询。
- 其他状态：继续轮询，建议间隔 2-5 秒。

## 5. 完整任务接口：`/v1/tasks`

需要使用内部任务协议时，可以直接指定任务类型：

```bash
curl -X POST "http://127.0.0.1:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "adobe_image2_workflow",
    "json": {
      "model": "adobe-gpt-image2-medium",
      "prompt": "A premium studio product photograph of a red ceramic vase",
      "quality": "medium",
      "aspect_ratio": "3:2"
    }
  }'
```

创建响应：

```json
{
  "success": true,
  "task_id": "TASK_ID"
}
```

查询：

```bash
curl "http://127.0.0.1:8000/v1/tasks/TASK_ID" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

完成时，核心结果在 `result` 中：

```json
{
  "task_id": "TASK_ID",
  "status": "completed",
  "progress": 100,
  "error_message": null,
  "result": {
    "type": "adobe_image2",
    "provider": "adobe",
    "workflow_kind": "image",
    "model": "adobe-gpt-image2-medium",
    "provider_model": "gpt-image@2",
    "quality": "medium",
    "aspect_ratio": "3:2",
    "generation_mode": "image2image",
    "reference_count": 1,
    "image_url": "/public/adobe-image2-assets/TASK_ID-0.png",
    "url": "/public/adobe-image2-assets/TASK_ID-0.png",
    "urls": [
      "/public/adobe-image2-assets/TASK_ID-0.png"
    ],
    "remaining_quota": 3980
  }
}
```

生产调用不要传 `mapping_id` 或 `window_pk`，让服务在所有已启用的 Adobe 窗口中
自动调度。这两个字段是内部管理标识，不属于下游透传协议；固定它们会绕过账号池，
并在账号迁移或重新绑定后失效。

## 6. 多账号调度规则

下游只需要传 Public Model ID，账号池调度由 fpbrowser2api 完成：

1. `adobe-gpt-image2-*` 被路由到 `adobe_image2_workflow`。
2. 服务从已启用、剩余额度可用、未处于错误冷却且有空闲并发的 Adobe 映射中选取窗口。
3. 任务绑定窗口后，在该窗口的 Adobe 登录会话中执行；同一窗口登录的其他平台账号
   不会参与 Adobe 任务，也不会被 Adobe 账号资料覆盖。
4. 生成成功后，服务更新该 Adobe 映射的剩余额度，并把图片保存到本地公开资源目录。
5. 某个 Adobe 账号被禁用、额度耗尽或登录失效时，下游请求格式不变；运维侧修复或
   增加映射即可恢复容量。

调度不保证账号粘性。下游不得假设连续两次请求会使用同一个 Adobe 账号，也不应
根据内部窗口编号实现业务逻辑。

## 7. Dry Run：检查账号和额度

Dry Run 会打开已登录的 Adobe 页面并读取账号状态，但不会点击 Generate，也不会
消耗生成额度。建议使用完整任务接口：

```bash
curl -X POST "http://127.0.0.1:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "adobe_image2_workflow",
    "json": {
      "model": "adobe-gpt-image2-medium",
      "aspect_ratio": "auto",
      "dry_run": true
    }
  }'
```

Dry Run 不要求 `prompt`。任务完成后的 `result` 示例：

```json
{
  "type": "adobe_image2_probe",
  "provider": "adobe",
  "workflow_kind": "image",
  "dry_run": true,
  "model": "adobe-gpt-image2-medium",
  "provider_model": "gpt-image@2",
  "quality": "medium",
  "aspect_ratio": "auto",
  "remaining_quota": 4000,
  "provisioned_quota": 4000,
  "plan_title": "Adobe Firefly Premium"
}
```

额度是创建任务时的快照。正式生成完成后，结果中的 `remaining_quota` 是服务再次
读取 Adobe 页面得到的剩余额度。

## 8. Python 下游示例

```python
import time
from urllib.parse import urljoin

import requests

BASE_URL = "http://127.0.0.1:8000"
API_KEY = "YOUR_FPBROWSER2API_KEY"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

created = requests.post(
    f"{BASE_URL}/v1/videos",
    headers=HEADERS,
    json={
        "model": "adobe-gpt-image2-medium",
        "prompt": "A premium studio product photograph of a red ceramic vase",
        "aspect_ratio": "3:2",
        # 删除 image 即为文生图；保留 image 会自动切换为图生图。
        "image": "https://cdn.example.com/input/vase.png",
    },
    timeout=30,
)
created.raise_for_status()
task_id = created.json()["task_id"]

while True:
    response = requests.get(
        f"{BASE_URL}/v1/videos/{task_id}",
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    task = response.json()

    if task["status"] == "completed":
        image_url = (
            task.get("image_url")
            or task.get("url")
            or (task.get("metadata", {}).get("result_urls") or [None])[0]
        )
        if not image_url:
            raise RuntimeError("completed task returned no image URL")
        absolute_image_url = urljoin(f"{BASE_URL}/", image_url)
        print(absolute_image_url)
        break

    if task["status"] == "failed":
        raise RuntimeError(task.get("error", {}).get("message") or "task failed")

    time.sleep(3)
```

## 9. 常见 HTTP 错误

| HTTP 状态码 | 含义 | 下游处理建议 |
|---:|---|---|
| `400` | 模型、比例、质量或提示词不合法 | 修正请求，不要原样重试 |
| `401` | fpbrowser2api API Key 缺失或错误 | 检查 `Authorization` 请求头 |
| `404` | 任务或图片资源不存在 | 检查 `task_id`、资源地址和服务实例 |
| `429` | 创建任务并发过高 | 指数退避后重试 |
| `500` | 服务内部错误 | 记录响应和 `task_id`，稍后重试或排查服务日志 |
| `503` | 服务暂停接收新任务 | 按响应提示稍后重试 |

任务创建成功后，即使 HTTP 查询为 `200`，仍需检查 JSON 中的 `status`；业务失败会
以 `status: "failed"` 和 `error`/`error_message` 返回。

## 10. 幂等、超时与重试

创建接口当前不接收下游自定义幂等键。下游应遵循以下规则，避免重复消耗 Adobe
额度：

1. 收到创建响应后立即持久化 `task_id`，后续只轮询状态，不要重复创建。
2. 已取得 `task_id` 时，任何查询超时都只重试 `GET`，不要再次调用创建接口。
3. 创建请求发生连接中断且未取得 `task_id` 时，结果具有歧义；不要无条件自动重放，
   应先根据下游自己的请求记录排查，或转人工确认。
4. HTTP `429` 或 `503` 且服务明确没有返回 `task_id` 时，可以指数退避后有限重试。
5. 业务状态已经是 `failed` 时，不要无限重试同一提示词；先记录错误原因。

建议创建请求 HTTP 超时设置为 30 秒，状态查询超时设置为 10-30 秒，轮询间隔为
2-5 秒。生成耗时不受创建请求 HTTP 超时限制。

## 11. 运行要求与下游建议

1. Adobe 对应指纹浏览器窗口必须保持启用，且 Firefly 登录会话有效。
2. 下游只保存 fpbrowser2api API Key，不保存 Adobe Token、Cookie 或账号密码。
3. 生产调用只使用 `adobe-gpt-image2-*` Public Model ID，不传 `mapping_id` 或 `window_pk`。
4. 创建接口配置 30 秒 HTTP 超时；生成过程通过状态接口异步轮询。
5. 对 `429`、`500`、`503` 做有限次数退避重试，对业务 `failed` 不做无限重试。
6. 完成后尽快把图片下载到下游自己的对象存储，避免长期依赖单台服务的本地文件。
7. Adobe 账号扩容时新增并启用同任务类型的窗口映射即可，下游请求协议无需变化。
8. 不要把 `mapping_id`、`window_pk`、Adobe Token、Cookie 或浏览器窗口编号写进下游配置。
