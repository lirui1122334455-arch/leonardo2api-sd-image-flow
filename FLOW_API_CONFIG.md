# Flow API 閫忎紶閰嶇疆鏂囨。

杩欎唤鏂囨。鐢ㄤ簬鍦ㄥ彟涓€鍙扮數鑴戜笂閰嶇疆璋冪敤鏈満鐨?Mirr 鐭╅樀 / fpbrowser2api 鏈嶅姟锛岄€氳繃鏈満宸茬粡鐧诲綍濂界殑 Flow 璐﹀彿鐢熸垚鍥剧墖鎴栬棰戙€?
## 1. 鏈嶅姟淇℃伅

鏈満灞€鍩熺綉鍦板潃锛?
```text
http://192.168.0.15:8000
```

鐢熸垚鎺ュ彛锛?
```text
POST http://192.168.0.15:8000/v1/videos
```

鏌ヨ浠诲姟鎺ュ彛锛?
```text
GET http://192.168.0.15:8000/v1/tasks/{task_id}
```

API Key锛?
```text
YOUR_API_KEY
```

璇锋眰澶达細

```text
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
```

璇存槑锛歚YOUR_API_KEY` 鍙槸 Mirr 鐭╅樀绯荤粺閰嶇疆閲岀殑璁块棶瀵嗛挜鍚嶇О锛屽悕瀛楅噷甯?`sora2` 涓嶄唬琛ㄤ細璧?Sora銆傜湡姝ｅ喅瀹氳蛋 Flow 鍝釜妯″瀷鐨勬槸璇锋眰浣撻噷鐨?`model` 瀛楁銆?
## 2. 妯″瀷閰嶇疆鎬昏

| Flow 妯″瀷 | 璇锋眰浣?model | 绫诲瀷 | 鏀寔鏃堕暱 | 鏀寔姣斾緥 | 澶囨敞 |
|---|---|---|---|---|---|
| Banana 2 | `nana-banana-2` | 鍥剧墖 | 涓嶅～ | `1:1` / `4:3` / `3:4` / `16:9` / `9:16` | Flow 鍐呴儴妯″瀷 `NARWHAL` |
| Banana Pro | `nana-banana-pro` | 鍥剧墖 | 涓嶅～ | `1:1` / `4:3` / `3:4` / `16:9` / `9:16` | Flow 鍐呴儴妯″瀷 `GEM_PIX_2` |
| Veo 3.1 Lite | `veo-3-1-lite` | 瑙嗛 | 鍥哄畾 `8` | `16:9` / `9:16` | 鏀寔鏂囩敓瑙嗛銆侀灏惧抚銆佸鍙傝€冨浘 |
| Veo 3.1 Fast | `veo-3-1-fast` | 瑙嗛 | 鍥哄畾 `8` | `16:9` / `9:16` | 鏀寔鏂囩敓瑙嗛銆侀灏惧抚銆佸鍙傝€冨浘锛沗veo-3-1` 鏄吋瀹规棫鍚?|
| Veo 3.1 Quality | `veo-3-1-quality` | 瑙嗛 | 鍥哄畾 `8` | `16:9` / `9:16` | 鏀寔鏂囩敓瑙嗛銆侀灏惧抚銆佸鍙傝€冨浘 |
| VEOomni / Gemini Omni Flash | `VEOomni` | 瑙嗛 | `4` / `6` / `8` / `10` | `16:9` / `9:16` | 鏀寔鏂囩敓瑙嗛銆佸鍙傝€冨浘 |

## 3. Banana 2

### 鏂囩敓鍥剧墖

```json
{
  "model": "nana-banana-2",
  "prompt": "a cute banana mascot wearing sunglasses, studio lighting",
  "aspect_ratio": "1:1",
  "resolution": "1k"
}
```

### 鍙傝€冨浘鐢熷浘鐗?
```json
{
  "model": "nana-banana-2",
  "prompt": "make a poster using these references",
  "aspect_ratio": "4:3",
  "resolution": "1k",
  "images": [
    "https://your-domain.com/ref.jpg"
  ]
}
```

## 4. Banana Pro

### 鏂囩敓鍥剧墖

```json
{
  "model": "nana-banana-pro",
  "prompt": "premium editorial product photo, luxury magazine style",
  "aspect_ratio": "16:9",
  "resolution": "2k"
}
```

### 鍙傝€冨浘鐢熷浘鐗?
```json
{
  "model": "nana-banana-pro",
  "prompt": "create a high-end product image using the references",
  "aspect_ratio": "1:1",
  "resolution": "2k",
  "images": [
    "https://your-domain.com/ref.jpg"
  ]
}
```

## 5. Veo 3.1 Lite / Fast / Quality

鍙～ `veo-3-1-lite`銆乣veo-3-1-fast`銆乣veo-3-1-quality`銆傛棫鍚?`veo-3-1` 浠嶅彲鐢紝绛夊悓浜?`veo-3-1-fast`銆?
### 鏂囩敓瑙嗛

```json
{
  "model": "veo-3-1-quality",
  "prompt": "a cinematic drone shot over a futuristic city at sunrise",
  "duration": 8,
  "aspect_ratio": "16:9"
}
```

### 棣栧熬甯х敓鎴愯棰?
`images` 鏀?1-2 寮犲浘銆? 寮犲浘琛ㄧず鍥剧敓瑙嗛锛? 寮犲浘琛ㄧず棣栧熬甯ц棰戙€?
```json
{
  "model": "veo-3-1-fast",
  "prompt": "animate smoothly from the first frame to the last frame",
  "duration": 8,
  "aspect_ratio": "9:16",
  "images": [
    "https://your-domain.com/first.jpg",
    "https://your-domain.com/last.jpg"
  ]
}
```

### 澶氬弬鑰冨浘鐢熸垚瑙嗛

Veo 3.1 澶氬弬鑰冨浘瑙嗛浣跨敤 `Ingredients_images`锛屾渶澶?3 寮犮€?
```json
{
  "model": "veo-3-1-lite",
  "prompt": "combine these references into a cinematic product launch video",
  "duration": 8,
  "aspect_ratio": "16:9",
  "Ingredients_images": [
    "https://your-domain.com/ref1.jpg",
    "https://your-domain.com/ref2.jpg",
    "https://your-domain.com/ref3.jpg"
  ]
}
```

## 6. VEOomni / Gemini Omni Flash

### 鏂囩敓瑙嗛

```json
{
  "model": "VEOomni",
  "prompt": "a cinematic product reveal with smooth camera movement",
  "duration": 4,
  "aspect_ratio": "16:9"
}
```

### 鍙傝€冨浘鐢熸垚瑙嗛

VEOomni 是多参考图模型：`images` 默认按多参考图处理，最多 3 张。
如果要强制走首帧/尾帧 I2V，请额外传 `video_mode: "i2v"`，此时 `images` 最多 2 张，顺序为 `[首帧, 尾帧]`。
```json
{
  "model": "VEOomni",
  "prompt": "use the reference image to create a cinematic product video",
  "duration": 4,
  "aspect_ratio": "16:9",
  "images": [
    "https://your-domain.com/ref-1.jpg",
    "https://your-domain.com/ref-2.jpg",
    "https://your-domain.com/ref-3.jpg"
  ]
}
```

`Ingredients_images` 仍然兼容，效果等同于 VEOomni 多参考图：

```json
{
  "model": "VEOomni",
  "prompt": "combine these references into a cinematic product video",
  "duration": 4,
  "aspect_ratio": "16:9",
  "Ingredients_images": [
    "https://your-domain.com/ref-1.jpg",
    "https://your-domain.com/ref-2.jpg"
  ]
}
```

## 7. 鎻愪氦浠诲姟绀轰緥

```bash
curl -X POST http://192.168.0.15:8000/v1/videos \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "VEOomni",
    "prompt": "a cinematic shot of a red sports car driving through rain at night",
    "duration": 4,
    "aspect_ratio": "16:9"
  }'
```

鎻愪氦鎴愬姛鍚庝細杩斿洖 `task_id`銆?
## 8. 鏌ヨ浠诲姟缁撴灉

鎶婃彁浜ゆ帴鍙ｈ繑鍥炵殑 `task_id` 濉繘鍘伙細

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  http://192.168.0.15:8000/v1/tasks/浣犵殑task_id
```

浠诲姟瀹屾垚鍚庯紝杩斿洖缁撴灉閲屼細鍖呭惈瑙嗛鎴栧浘鐗囧湴鍧€銆?
## 9. 甯歌濉啓鏂瑰紡

濡傛灉瀵规柟宸ュ叿鎶?Base URL 鍜?Endpoint 鍒嗗紑濉細

```text
Base URL:
http://192.168.0.15:8000

Endpoint:
/v1/videos

API Key:
YOUR_API_KEY

Header:
Authorization: Bearer YOUR_API_KEY

Model:
VEOomni
```

濡傛灉瀵规柟宸ュ叿瑕佹眰濉啓瀹屾暣 URL锛?
```text
http://192.168.0.15:8000/v1/videos
```

## 10. 娉ㄦ剰浜嬮」

- 鍙︿竴鍙扮數鑴戝繀椤诲拰杩欏彴鐢佃剳鍦ㄥ悓涓€涓眬鍩熺綉銆?- 濡傛灉鍙︿竴鍙扮數鑴戣闂笉浜嗭紝鍏堟鏌ユ湰鏈?VPN 鏄惁寮€鍚簡闃绘灞€鍩熺綉璁块棶銆?- 娴嬭瘯 VEOomni 鏃跺彲鍏堢敤 `duration=4`锛屾秷鑰楁洿灏戙€?- Banana 2 / Banana Pro 鏄浘鐗囨ā鍨嬶紝涓嶉渶瑕佸～鍐?`duration`銆?- Veo 3.1 Lite / Fast / Quality 鍥哄畾濉啓 `duration: 8`锛屼笉濉椂鏈嶅姟绔篃浼氭寜 8 澶勭悊銆?- VEOomni 鏀寔 `duration: 4 / 6 / 8 / 10`銆?- 濡傛灉鍚庣画鍦?Mirr 鐭╅樀閲屼慨鏀逛簡 API Key锛屽彟涓€鍙扮數鑴戠殑 `Authorization: Bearer ...` 涔熻鍚屾淇敼銆?
