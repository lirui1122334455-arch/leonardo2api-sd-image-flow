# Fish Audio 内网透传接入文档

更新时间：2026-08-07

## 1. 接入地址

当前 fpbrowser2api 服务信息：

| 项目 | 值 |
|---|---|
| 内网服务器 IP | `192.168.0.15` |
| 服务端口 | `8000` |
| 内网 Base URL | `http://192.168.0.15:8000` |
| 创建任务 | `POST /v1/tasks` |
| 查询任务 | `GET /v1/tasks/{task_id}` |
| 任务类型 | `fish_audio_workflow` |
| 调用方式 | 异步提交、轮询结果 |

已验证服务监听 `0.0.0.0:8000`，并且内网地址
`http://192.168.0.15:8000/admin/test` 可以访问。

调用方必须和服务器位于可互通的内网中，并确保服务器 Windows 防火墙允许
TCP `8000` 入站。`192.168.0.15` 是当前 DHCP 地址；如果路由器重新分配 IP，
需要同步更新调用方 Base URL，建议在路由器中为服务器配置 DHCP 地址保留。

## 2. 鉴权

所有 `/v1/*` 请求都需要使用 fpbrowser2api 自己的 API Key：

```http
Authorization: Bearer YOUR_FPBROWSER2API_KEY
Content-Type: application/json
```

这里不能填写 Fish 官方 API Key。当前实现使用已登录的 Fish 指纹浏览器会话，
不会读取或保存 Fish 官方 API Key。

## 3. 创建音频任务

### 完整请求

```bash
curl -X POST "http://192.168.0.15:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "fish_audio_workflow",
    "json": {
      "text": "欢迎使用 Fish Audio 内网语音生成服务。",
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

### 最简请求

```bash
curl -X POST "http://192.168.0.15:8000/v1/tasks" \
  -H "Authorization: Bearer YOUR_FPBROWSER2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type_code": "fish_audio_workflow",
    "json": {
      "text": "这是一段测试语音。",
      "reference_id": "5c353fdb312f4888836a9a5680099ef0"
    }
  }'
```

成功响应：

```json
{
  "success": true,
  "task_id": "56ea9d536ea74a0fa0c79ed5e90493ad"
}
```

## 4. 请求字段

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_type_code` | string | 是 | 固定为 `fish_audio_workflow` |
| `json` | object | 是 | Fish Audio TTS 参数 |
| `mapping_id` | integer | 否 | 指定任务映射；不传时自动选择窗口 |
| `window_pk` | integer | 否 | 指定本地执行窗口；通常不传 |

### `json` 字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `text` | string | 是 | - | 朗读文本；也支持别名 `prompt` |
| `reference_id` | string | 是 | - | 音色 ID，见本文音色表 |
| `backend` | string | 否 | `s2.1-pro` | Fish 合成后端版本 |
| `format` | string | 否 | `mp3` | Fish 网页任务接口支持 `mp3`、`pcm` |
| `speed` | number | 否 | Fish 默认值 | 语速 |
| `volume` | number | 否 | Fish 默认值 | 音量 |
| `normalize_loudness` | boolean | 否 | Fish 默认值 | 是否进行响度标准化 |
| `temperature` | number | 否 | Fish 默认值 | 采样温度 |
| `top_p` | number | 否 | Fish 默认值 | nucleus sampling 参数 |
| `latency` | string | 否 | `balanced` | 延迟策略 |
| `prosody` | object | 否 | `{}` | 其他韵律参数透传对象 |
| `sampler` | object | 否 | `{}` | 其他采样参数透传对象 |

音色 ID 还接受 `voice_id`、`model_id`、`model` 三个别名，但建议接入方统一使用
`reference_id`。

Fish 网页请求没有独立的 `country` 或 `language` 生成字段。朗读语言由 `text`
内容决定，音色表中的语言表示该音色更适合的语言或口音。

## 5. 查询任务

```bash
curl "http://192.168.0.15:8000/v1/tasks/56ea9d536ea74a0fa0c79ed5e90493ad" \
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

生成完成：

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
    "reference_id": "5c353fdb312f4888836a9a5680099ef0",
    "backend": "s2.1-pro",
    "format": "mp3"
  },
  "error_message": null
}
```

调用方应每 1-3 秒查询一次，直到 `status` 为 `completed` 或 `failed`。
成功后优先读取：

```text
audio_url
result.audio_url
url
result.url
```

## 6. Python 接入示例

```python
import time

import requests


BASE_URL = "http://192.168.0.15:8000"
API_KEY = "YOUR_FPBROWSER2API_KEY"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

created = requests.post(
    f"{BASE_URL}/v1/tasks",
    headers=HEADERS,
    json={
        "task_type_code": "fish_audio_workflow",
        "json": {
            "text": "欢迎使用内网语音生成接口。",
            "reference_id": "5c353fdb312f4888836a9a5680099ef0",
            "backend": "s2.1-pro",
            "format": "mp3",
        },
    },
    timeout=30,
)
created.raise_for_status()
task_id = created.json()["task_id"]

while True:
    response = requests.get(
        f"{BASE_URL}/v1/tasks/{task_id}",
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    task = response.json()
    if task["status"] == "completed":
        print(task.get("audio_url") or task["result"]["audio_url"])
        break
    if task["status"] == "failed":
        raise RuntimeError(task.get("error_message") or "Fish Audio 生成失败")
    time.sleep(2)
```

## 7. 音色 ID 列表

年龄段来自 Fish 平台标签：`young=青年`、`middle-aged=中年`、`old=年长`，
不是精确年龄。公开社区音色可能被作者删除、转私有或下架。

### 中文

| 音色名称 | `reference_id` | 性别 | 年龄段 | 特点 |
|---|---|---|---|---|
| 王琨声音模型10.17t1 | `4f201abba2574feeae11e5ebf737859e` | 男 | 中年 | 专业、清晰、干脆、播音、主持 |
| 丁真 | `54a5170264694bfc8e9ad98df7bd89c3` | 男 | 青年 | 友好、平静、温柔、柔和、普通话 |
| 央视配音 | `59cb5986671546eaa6ca8ae6f29f6d22` | 男 | 年长 | 教育、专业、清晰、自信、纪录片 |
| AD学姐 | `7f92f8afb8ec43bf81429cc1c9199cb1` | 女 | 青年 | 女友感、御姐、舒缓、低沉、严肃 |
| 陶衿 | `b7f6ea6bf21246de894f6b9b499add43` | 女 | 中年 | 教育、平静、温柔、专业、清晰 |
| 王鑫 | `c1e4dc2e84ac448699b865e4e9e790a1` | 女 | 年长 | 教育、专业、自信、清晰、普通话 |

### 英语

| 音色名称 | `reference_id` | 性别 | 年龄段 | 特点 |
|---|---|---|---|---|
| modal-1 | `0429f2b252464b88b2ab2128f084290c` | 女 | 年长 | 教育、平静、匀速、专业、清晰 |
| ALEX_CHIKNA | `52e0660e03fe4f9a8d2336f67cab5440` | 男 | 中年 | 活力、自信、热情、快速、体育解说 |
| Energetic Male | `802e3bc2b27e49c2995d23ef70e6ac89` | 男 | 青年 | 活力、热情、清晰、干脆、播音 |
| Super Smash Bros. 4/Ultimate Announcer | `90e65eaaf50e4470b8e6d43ee6afd7d5` | 男 | 年长 | 低沉、活力、权威、戏剧化、播音 |
| Sarah | `933563129e564b19a115bedd57b7406a` | 女 | 青年 | 对话感、柔和、气声、亲密、温柔 |
| Jasphina | `e9b134e4c0b547a3894793be502314f1` | 女 | 中年 | 活力、俏皮、动画感、表现力、快速 |

### 日语和韩语

| 音色名称 | `reference_id` | 性别 | 年龄段 | 语言 | 特点 |
|---|---|---|---|---|---|
| announcer Super Smash Bros | `1dc6c2dc086b48c4b2a7f943401059b6` | 男 | 年长 | 英语/日语/韩语 | 中音、活力、权威、戏剧化、专业 |
| 元気な女性 | `5161d41404314212af1254556477c17d` | 女 | 青年 | 日语 | 对话感、温柔、友好、明亮、顺滑 |
| ほしVer3.0 | `54fa0418415a4103885ec909023b0285` | 男 | 中年 | 日语 | 活力、快速、热情、清晰 |
| 士道 | `8f99ad75c8184f1db0c21d3a906445a4` | 男 | 青年 | 日语 | 教育、平静、专业、温柔、匀速 |
| 田中みなみ風 | `ac870f5b0f7e45609b4e8d79bc4082ff` | 女 | 中年 | 日语 | 专业、温暖、主持、顺滑 |
| Jisoo | `13247dc70a3a49dd8b3dacf0a4841b34` | 女 | 中年 | 韩语 | 对话感、柔和、平静、温柔、气声 |
| 보이스1 | `8cf5ee4cb0224c109852a206f185a05f` | 男 | 中年 | 韩语 | 对话感、明亮、活力、热情 |
| 卢正义 노정의 | `a86d9eac550d4814b9b4f6fc53661930` | 女 | 青年 | 韩语 | 教育、平静、专业、顺滑 |
| Till^^ (Alien stage) | `d9708e800c754f0c876953e74d846f5a` | 男 | 青年 | 英语/韩语 | 暗黑、沙哑、神秘、戏剧化、反派感 |

### 西班牙语和法语

| 音色名称 | `reference_id` | 性别 | 年龄段 | 语言 | 特点 |
|---|---|---|---|---|---|
| Kasane Teto (español) | `0118a35dcb604837abe7961a43e13ba8` | 女 | 中年 | 西班牙语 | 活力、角色感、友好、低沉、自信 |
| Idea Vilariño | `26ff45fab722431c85eea2536e5c5197` | 女 | 年长 | 西班牙语 | 低沉、严肃、戏剧化、亲密、放松 |
| Lionel Messi | `30ca8b4d162e463d818bb99101f4857e` | 男 | 青年 | 西班牙语 | 活力、自信、权威、教练感、清晰 |
| Valentino Narración Biblica Fer | `8d2c17a9b26d4d83888ea67a1ee565b2` | 男 | 年长 | 西班牙语 | 平静、严肃、权威、专业、清晰 |
| Hatsune Miku (Text To Speech) | `acc8237220d8470985ec9be6c4c480a9` | 女 | 青年 | 西班牙语/英语 | 活力、欢快、明亮、友好、动漫 |
| Farid Dieck | `dfa5b230c8054f429e434f4a6e9bbdec` | 男 | 中年 | 西班牙语 | 温暖、平静、匀速、表现力、故事感 |
| Steal a Brainrot (Brainrots Voice) | `1e17dc57eaba4f27ac3ee8d50cb8d040` | 男 | 中年 | 多语言 | 活力、欢快、俏皮、动画感、节奏感 |
| Le narrateur | `4f2a0684dd0247dda68f339738c780e6` | 男 | 年长 | 法语 | 低沉、低音、电影感、神秘 |
| Féminine | `5567200c7d8341738f0892bbacd3be3c` | 女 | 中年 | 法语 | 对话感、平静、专业、低音、顺滑 |
| Voix Narrative Française | `588134063cd047ffafc43114f3f26746` | 男 | 青年 | 法语 | 教育、中音、清晰、专业、平静 |
| Fille | `690813f2df56491b82ee02a22d1c67fd` | 女 | 青年 | 法语 | 对话感、柔和、气声、放松、亲密 |
| marine le pen | `fa6983af3ca24b9a844a0866af745684` | 女 | 年长 | 法语 | 专业、严肃、自信、权威 |

### 德语、俄语和阿拉伯语

| 音色名称 | `reference_id` | 性别 | 年龄段 | 语言 | 特点 |
|---|---|---|---|---|---|
| Metal Sonic | `13533a60000342348698dda798564e72` | 男 | 年长 | 德语/西班牙语/英语 | 中性、匀速、机器人、金属感、科幻 |
| Nachrichtensendung | `285f00e53ff14eb1b111532fb39569d3` | 女 | 年长 | 德语 | 专业、清晰、匀速、中性、权威 |
| Christa deutsch | `88b18e0d81474a0ca08e2ea6f9df5ff4` | 女 | 青年 | 德语 | 教育、明亮、清晰、友好、专业 |
| Vorlesen Stimlagen | `90042f762dbf49baa2e7776d011eee6b` | 男 | 中年 | 德语 | 教育、平静、匀速、清晰、专业 |
| Catrinja | `c5b66a80d90749fc914c714e793d1a2f` | 女 | 中年 | 德语 | 对话感、柔和、气声、亲密、放松 |
| Ingo Ruff | `dc17e99b25e14686b107caa1d270c802` | 男 | 青年 | 德语 | 低沉、清晰、匀速、专业、播音 |
| Меллстрой | `0a690dbeb3984a9f88cd39353880775f` | 男 | 中年 | 俄语 | 活力、自信、动感、快速 |
| Спокойный женский голос | `2a1036d645634680b3cc69aeeb60375b` | 女 | 年长 | 俄语 | 中音、平静、匀速、顺滑、清晰 |
| Mita Miside (Russian voice) | `6dc11f3f67a543f6ad4537a4a347e224` | 女 | 青年 | 英语/俄语 | 俄语、游戏、角色感 |
| Молодой Аналитик | `868377a7b08f4c0d9acf8c9f059571aa` | 男 | 青年 | 俄语 | 教育、清晰、专业、严肃、匀速 |
| Женский голос | `aa615eaff73f417e91cfbb4ea0e42df8` | 女 | 中年 | 俄语 | 中音、顺滑、平静、表现力、故事感 |
| Жириновский | `bc8eb8dcdc184763b0a769ee03275724` | 男 | 年长 | 俄语 | 愤怒、戏剧化、电影感、游戏 |
| غامبول | `1d51fdd65ff14342aec4dffa0ef58386` | 男 | 青年 | 阿拉伯语 | 教育、活力、清晰、友好 |
| ميماتي | `33ceed22a1ff4c15b5a7d4b0617e21b8` | 男 | 中年 | 阿拉伯语 | 高音、低沉、沙哑、严肃、权威 |
| قثيض | `564ff4b232d6427f91513321de5fb651` | 女 | 中年 | 阿拉伯语 | 活力、专业、清晰、干脆 |
| عصام الشوالي | `5b67899dc9a34685ae09c94c890a606f` | 男 | 年长 | 阿拉伯语 | 活力、动感、播音、表现力 |
| يي | `7eee0787bf1a476fb0864270853e344a` | 女 | 青年 | 阿拉伯语 | 柔和、气声、温柔、亲密 |

## 8. 错误处理

| HTTP/状态 | 原因 | 处理方式 |
|---|---|---|
| `400` | 文本为空、音色 ID 缺失、格式错误 | 检查请求字段 |
| `401` | fpbrowser2api API Key 错误，或 Fish 登录过期 | 检查鉴权；重新登录指纹窗口 |
| `403` | 音色不可访问或 Fish 拒绝请求 | 更换音色并检查账号状态 |
| `422` | Fish 生成失败 | 检查文本、音色和平台限制 |
| `429` | 提交过于频繁 | 指数退避后重试 |
| `502` | 浏览器会话或 Fish 请求异常 | 检查窗口和网络状态 |
| `504` | 生成超时 | 查询任务状态或重新提交 |

## 9. 生产使用注意事项

- Fish 指纹浏览器账号必须保持登录，对应任务映射必须启用。
- 社区音色可能随时失效，生产环境应维护经过测试和授权的音色白名单。
- 使用真人、名人或角色音色前，应确认人格权、著作权和平台授权。
- Fish 返回的对象存储地址可能不是永久地址，业务方应及时转存生成音频。
- 不要把 `8000` 端口直接暴露到公网；如需跨公网调用，应增加 HTTPS、访问控制、
  限流和反向代理。
