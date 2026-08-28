# Fish Audio TTS 透传接口文档

更新时间：2026-08-06

## 1. 接口说明

本项目通过已登录 Fish Audio 的指纹浏览器窗口提交 TTS 任务，不使用、
不保存 Fish 官方 API Key。调用方只需要使用 fpbrowser2api 自己的 API Key。

- Base URL：`http://127.0.0.1:8000`（部署后替换成实际地址）
- 创建任务：`POST /v1/tasks`
- 查询任务：`GET /v1/tasks/{task_id}`
- 鉴权：`Authorization: Bearer <FPBROWSER2API_API_KEY>`
- 任务类型：`fish_audio_workflow`
- 调用方式：异步创建、轮询结果

## 2. 创建任务

```bash
curl -X POST "http://127.0.0.1:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "fish_audio_workflow",
    "json": {
      "text": "欢迎使用 Fish Audio 语音生成服务。",
      "reference_id": "5c353fdb312f4888836a9a5680099ef0",
      "backend": "s2.1-pro",
      "format": "mp3",
      "speed": 1.0,
      "volume": 0,
      "normalize_loudness": true,
      "temperature": 0.7,
      "top_p": 0.9,
      "latency": "balanced"
    }
  }'
```

成功创建：

```json
{
  "success": true,
  "task_id": "56ea9d536ea74a0fa0c79ed5e90493ad"
}
```

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_type_code` | string | 是 | 固定为 `fish_audio_workflow` |
| `json` | object | 是 | Fish Audio TTS 透传参数 |
| `mapping_id` | integer | 否 | 指定任务类型映射；一般不传，由调度器选择 |
| `window_pk` | integer | 否 | 指定本地窗口；一般不传，由调度器选择 |

### `json` 字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `text` | string | 是 | - | 要合成的文本；也可使用别名 `prompt` |
| `reference_id` | string | 建议必填 | 最近使用音色 | Fish 音色 ID |
| `voice_id` | string | 否 | - | `reference_id` 的别名 |
| `model_id` | string | 否 | - | `reference_id` 的别名 |
| `model` | string | 否 | - | `reference_id` 的别名，不是后端版本 |
| `backend` | string | 否 | `s2.1-pro` | Fish 合成后端版本；也可使用别名 `version` |
| `format` | string | 否 | `mp3` | Fish 网页任务接口支持 `mp3`、`pcm`；也可使用 `response_format` |
| `speed` | number | 否 | Fish 默认值 | 语速，写入 `prosody.speed` |
| `volume` | number | 否 | Fish 默认值 | 音量，写入 `prosody.volume` |
| `normalize_loudness` | boolean | 否 | Fish 默认值 | 响度标准化，写入 `prosody.normalize_loudness` |
| `prosody` | object | 否 | `{}` | 原样传递韵律参数；同名顶层字段会覆盖对象中的值 |
| `temperature` | number | 否 | Fish 默认值 | 采样温度，写入 `sampler.temperature` |
| `top_p` | number | 否 | Fish 默认值 | nucleus sampling，写入 `sampler.top_p` |
| `sampler` | object | 否 | `{}` | 原样传递采样参数；同名顶层字段会覆盖对象中的值 |
| `latency` | string | 否 | `balanced` | 延迟策略，当前网页默认使用 `balanced` |
| `normalize` | boolean | 否 | `false` | Fish 请求的文本/音频标准化开关 |
| `group_id` | string | 否 | - | Fish 任务分组 ID |

只需满足以下规则之一即可指定音色，优先级从左到右：

```text
reference_id > voice_id > model_id > model
```

建议调用方固定使用 `reference_id`，避免与 `backend` 的概念混淆。

## 3. 选择语言和音色

当前 Fish 网页生成请求没有独立的 `country` 或 `language` 参数：

1. 音色由 `reference_id` 决定。
2. 实际朗读语言主要由 `text` 内容决定。
3. 音色目录中的语言表示该音色的推荐/训练语言，不会强制限制输入文本。
4. 需要口音或地区差异时，应选择对应语言、口音标签的音色 ID。

例如，选择中文女声：

```json
{
  "task_type_code": "fish_audio_workflow",
  "json": {
    "text": "今天的天气很好，我们一起出发吧。",
    "reference_id": "5c353fdb312f4888836a9a5680099ef0"
  }
}
```

随附的 `FISH_AUDIO_VOICE_CATALOG.csv` 是可直接筛选的音色目录。年龄列是
Fish 平台标签映射：`young=青年`、`middle-aged=中年`、`old=年长`，不是精确年龄。

## 4. 查询任务

```bash
curl "http://127.0.0.1:8000/v1/tasks/56ea9d536ea74a0fa0c79ed5e90493ad" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

运行中：

```json
{
  "task_id": "56ea9d536ea74a0fa0c79ed5e90493ad",
  "status": "running",
  "progress": 40,
  "result": null,
  "error_message": null
}
```

完成：

```json
{
  "task_id": "56ea9d536ea74a0fa0c79ed5e90493ad",
  "status": "completed",
  "progress": 100,
  "audio_url": "https://platform.r2.fish.audio/task/xxx.mp3",
  "url": "https://platform.r2.fish.audio/task/xxx.mp3",
  "result": {
    "type": "fish_audio_tts",
    "provider": "fish_audio",
    "workflow_kind": "audio",
    "audio_url": "https://platform.r2.fish.audio/task/xxx.mp3",
    "share_url": "https://platform.r2.fish.audio/task/xxx.mp3",
    "url": "https://platform.r2.fish.audio/task/xxx.mp3",
    "urls": ["https://platform.r2.fish.audio/task/xxx.mp3"],
    "fish_task_id": "b1f5955b82544533b41a26aa5281b584",
    "reference_id": "5c353fdb312f4888836a9a5680099ef0",
    "backend": "s2.1-pro",
    "format": "mp3"
  },
  "error_message": null
}
```

失败：

```json
{
  "task_id": "56ea9d536ea74a0fa0c79ed5e90493ad",
  "status": "failed",
  "progress": 0,
  "result": null,
  "error_message": "Fish Audio login expired"
}
```

建议每 1-3 秒轮询一次，直到 `status` 为 `completed` 或 `failed`。

## 5. 常见错误

| HTTP/状态 | 原因 | 处理方式 |
|---|---|---|
| `400` | 文本为空、音色 ID 缺失、格式不支持或字段类型错误 | 检查请求字段 |
| `401` | fpbrowser2api 鉴权失败，或 Fish 登录态过期 | 区分接口返回内容；重新登录对应指纹窗口 |
| `403` | Fish 拒绝请求或音色不可访问 | 更换可访问音色并检查账号状态 |
| `422` | Fish 生成任务失败 | 检查文本、音色和平台限制 |
| `429` | 本服务创建任务过于频繁 | 退避后重试 |
| `502` | 浏览器会话或 Fish 请求异常 | 检查窗口状态与网络 |
| `504` | 生成超时 | 查询 Fish 状态或重新提交 |

## 6. 使用限制

- 指纹浏览器中的 Fish 账号必须保持登录，窗口映射必须启用。
- 网页授权头只缓存在服务进程内存中，不会写入数据库或文档。
- 社区公开音色可能被作者删除、转私有或下架，生产环境应保存自己的白名单。
- 使用真人、名人或角色音色前，应确认授权、人格权、著作权和平台条款。
- 音频结果 URL 由 Fish 对象存储返回，调用方应按业务需要及时转存。
