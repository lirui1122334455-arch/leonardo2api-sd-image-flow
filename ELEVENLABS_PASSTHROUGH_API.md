# ElevenLabs 浏览器会话透传接口

更新时间：2026-08-18

本接口复用已登录 ElevenLabs 的指纹浏览器窗口生成音效或语音，不要求调用方
提供 ElevenLabs API Key。网页授权头只缓存在服务进程内存中，生成音频会保存
到本机 `data/elevenlabs_assets`，任务查询接口返回可直接下载的 URL。

## 基本信息

- Base URL：`http://127.0.0.1:8000`（部署后替换为实际地址）
- 创建任务：`POST /v1/tasks`
- 查询任务：`GET /v1/tasks/{task_id}`
- 鉴权：`Authorization: Bearer <FPBROWSER2API_API_KEY>`
- 任务类型：`elevenlabs_workflow`

## 音效生成

```bash
curl -X POST "http://127.0.0.1:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "elevenlabs_workflow",
    "json": {
      "mode": "sound_effects",
      "prompt": "gentle rain tapping on a glass window",
      "duration_seconds": 3,
      "prompt_influence": 0.3,
      "loop": false,
      "model_id": "eleven_text_to_sound_v2",
      "number_of_generations": 4
    }
  }'
```

音效参数：

| 字段 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `mode` | 否 | `sound_effects` | 支持 `sound_effects`、`sfx` |
| `prompt` / `text` | 是 | - | 音效描述，最多 450 字符 |
| `duration_seconds` | 否 | 平台自动 | `0.5` 到 `30` 秒 |
| `prompt_influence` | 否 | `0.3` | `0` 到 `1` |
| `loop` | 否 | `false` | 是否生成可循环音效 |
| `model_id` | 否 | `eleven_text_to_sound_v2` | 也支持账号已开放的 v3 |
| `number_of_generations` | 否 | `4` | 默认与网页一致生成 4 个候选；可显式传 `1` 到 `4` |

## 文字转语音

```bash
curl -X POST "http://127.0.0.1:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "elevenlabs_workflow",
    "json": {
      "mode": "tts",
      "text": "欢迎使用 ElevenLabs 浏览器会话接口。",
      "voice_id": "YOUR_VOICE_ID",
      "model_id": "eleven_multilingual_v2",
      "output_format": "mp3_44100_128",
      "stability": 0.5,
      "similarity_boost": 0.75,
      "speed": 1.0,
      "use_speaker_boost": true
    }
  }'
```

`voice_id` 可省略；省略时执行器读取当前账号音色列表并使用第一个音色。可通过
`voice_settings` 对象原样传递 ElevenLabs 音色设置，顶层的 `stability`、
`similarity_boost`、`style`、`speed`、`use_speaker_boost` 会覆盖同名值。

## 查询结果

```bash
curl "http://127.0.0.1:8000/v1/tasks/TASK_ID" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY"
```

完成示例：

```json
{
  "task_id": "TASK_ID",
  "status": "completed",
  "progress": 100,
  "audio_url": "http://127.0.0.1:8000/public/elevenlabs-assets/TASK_ID-0.mp3",
  "result_urls": [
    "http://127.0.0.1:8000/public/elevenlabs-assets/TASK_ID-0.mp3"
  ],
  "result": {
    "provider": "elevenlabs",
    "workflow_kind": "audio",
    "mode": "sound_effects"
  }
}
```

音效模式可能一次返回多个文件，此时全部地址位于 `result_urls` 和
`result.urls`。音频文件路由无需 ElevenLabs 鉴权；请按业务需要配置网络访问
控制和定期清理策略。

## 运行要求

- ElevenLabs 指纹窗口必须保持有效登录态。
- 窗口需要绑定到已启用的 `elevenlabs_workflow` 任务类型。
- 服务进程需要对 `data/elevenlabs_assets` 有写权限。
- 网页接口或平台风控发生变化时，可能需要重新登录或更新执行器。
