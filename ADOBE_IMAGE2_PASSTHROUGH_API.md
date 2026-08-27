# Adobe Firefly GPT Image 2 透传 API

更新时间：2026-08-27

本接口复用已经登录 Adobe Firefly 的指纹浏览器窗口生成图片。下游不需要提供
Adobe Token、Cookie 或账号信息，只需要使用 fpbrowser2api 自己的 API Key。

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
| `aspect_ratio` | string | 否 | `auto` | `auto`、`3:2`、`1:1`、`2:3` |
| `quality` | string | 否 | 由模型决定 | `low`、`medium`、`high`；推荐直接用模型后缀选择 |
| `dry_run` | boolean | 否 | `false` | 只检查会话和额度，不点击生成、不消耗生成额度 |

注意：

1. 推荐通过 `model` 后缀选择质量，不要同时传入相互冲突的 `model` 和 `quality`。
2. Adobe 路由会把兼容字段 `duration` 固定为 `1`，下游可以不传。
3. 当前只支持文生图，不接收参考图、蒙版或图生图字段。
4. 生成消耗 Adobe 账号额度；实际扣减以 Adobe 页面显示为准。

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

### 4.2 查询任务

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
  "image_url": "http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png",
  "url": "http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png",
  "video_url": null,
  "metadata": {
    "result_urls": [
      "http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png"
    ]
  }
}
```

为兼容不同版本，下游按以下优先级读取结果：

1. `image_url`
2. `url`
3. `metadata.result_urls[0]`

返回的是 fpbrowser2api 本地持久化后的公开图片地址，不需要携带 Adobe Cookie。
跨机器部署时，确保响应中的服务主机名和 `/public/adobe-image2-assets/` 路径可被
下游访问。

### 4.3 失败结果

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
    "image_url": "http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png",
    "url": "http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png",
    "urls": [
      "http://127.0.0.1:8000/public/adobe-image2-assets/TASK_ID-0.png"
    ],
    "remaining_quota": 3980
  }
}
```

生产调用不要传 `mapping_id`，让服务在所有已启用的 Adobe 窗口中调度。只有排查
当前账号时才可临时在根对象加入 `"mapping_id": 68`；该编号属于当前部署环境，
下游不能把它作为长期协议的一部分。

## 6. Dry Run：检查账号和额度

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

## 7. Python 下游示例

```python
import time

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
        print(image_url)
        break

    if task["status"] == "failed":
        raise RuntimeError(task.get("error", {}).get("message") or "task failed")

    time.sleep(3)
```

## 8. 常见 HTTP 错误

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

## 9. 运行要求与下游建议

1. Adobe 对应指纹浏览器窗口必须保持启用，且 Firefly 登录会话有效。
2. 下游只保存 fpbrowser2api API Key，不保存 Adobe Token、Cookie 或账号密码。
3. 生产调用只使用 `adobe-gpt-image2-*` Public Model ID，不传 `mapping_id`。
4. 创建接口配置 30 秒 HTTP 超时；生成过程通过状态接口异步轮询。
5. 对 `429`、`500`、`503` 做有限次数退避重试，对业务 `failed` 不做无限重试。
6. 完成后尽快把图片下载到下游自己的对象存储，避免长期依赖单台服务的本地文件。
7. Adobe 账号扩容时新增并启用同任务类型的窗口映射即可，下游请求协议无需变化。

