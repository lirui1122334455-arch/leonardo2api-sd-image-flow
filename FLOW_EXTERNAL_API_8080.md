# Flow 澶栫綉 API 鎺ュ叆鏂囨。锛?080 绔彛锛?
杩欎唤鏂囨。鐢ㄤ簬鍦ㄥ彟涓€鍙扮數鑴戙€佸墠绔伐鍏锋垨涓浆闈㈡澘閲岃皟鐢ㄥ綋鍓嶈繖鍙扮數鑴戜笂鐨?Flow / fpbrowser2api 鏈嶅姟锛岀敓鎴愬浘鐗囨垨瑙嗛銆?
## 1. 鏈嶅姟淇℃伅

Base URL锛?
```text
http://103.218.243.87:8080
```

鎻愪氦鐢熸垚浠诲姟锛?
```text
POST http://103.218.243.87:8080/v1/videos
```

鏌ヨ浠诲姟缁撴灉锛屼紭鍏堜娇鐢細

```text
GET http://103.218.243.87:8080/v1/videos/{task_id}
```

鍏煎鏌ヨ鎺ュ彛锛?
```text
GET http://103.218.243.87:8080/v1/tasks/{task_id}
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

濡傛灉瀵规柟宸ュ叿鍒嗗紑濉啓锛?
```text
Base URL: http://103.218.243.87:8080
Endpoint: /v1/videos
API Key: YOUR_API_KEY
```

濡傛灉瀵规柟宸ュ叿瑕佹眰濉啓瀹屾暣鎺ュ彛鍦板潃锛?
```text
http://103.218.243.87:8080/v1/videos
```

娉ㄦ剰锛欰PI Key 閲岀殑 `sora2` 鍙槸瀵嗛挜鍚嶅瓧锛屼笉浠ｈ〃浼氳蛋 Sora銆傚疄闄呰蛋鍝釜 Flow 妯″瀷鐢辫姹備綋閲岀殑 `model` 瀛楁鍐冲畾銆?
## 2. 寮傛璋冪敤娴佺▼

鎵€鏈夌敓鎴愭帴鍙ｉ兘鏄紓姝ヤ换鍔★細

1. 璋冪敤 `POST /v1/videos` 鎻愪氦浠诲姟銆?2. 鎺ュ彛绔嬪嵆杩斿洖 `task_id`锛屾鏃朵换鍔￠€氬父鏄?`queued`銆?3. 姣忛殧 3-5 绉掕皟鐢?`GET /v1/videos/{task_id}` 杞銆?4. 褰?`status` 鍙樻垚 `completed` 鏃讹紝浠庤繑鍥炰綋璇诲彇鐢熸垚鍦板潃銆?5. 褰?`status` 鍙樻垚 `failed` 鏃讹紝璇诲彇 `error.message` 鎴?`error_message` 鍒ゆ柇澶辫触鍘熷洜銆?
璐﹀彿鍒嗛厤绛栫暐锛?
- 榛樿鑷姩浠庡凡鍚敤鐨?Flow 绐楀彛璐﹀彿閲岄€夋嫨鍙敤璐﹀彿銆?- 瑙嗛/鍥剧墖浠诲姟寮€濮嬪墠浼氬仛涓€娆¤交閲忎細璇濋妫€锛涘畠鍙鍙?Flow 鐨勭櫥褰曚細璇濓紝涓嶄細鐧诲綍銆佷笉浼氱偣鐢熸垚銆?- 濡傛灉鏌愪釜璐﹀彿浼氳瘽澶辨晥锛屾湇鍔′細绂佺敤璇ヨ处鍙风獥鍙ｅ苟鑷姩鍒囨崲鍏跺畠鍙敤璐﹀彿閲嶈瘯锛岄伩鍏嶅崟涓獥鍙ｆ帀绾垮鑷翠换鍔＄洿鎺ュけ璐ャ€?- 鍚庡彴杩樻湁浣庨闅忔満鍋ュ悍妫€鏌ワ細榛樿 90-150 鍒嗛挓鍙鏌?1 涓凡鍚敤 Flow 绐楀彛锛屼笖鍙湪鎻掍欢宸茬粡杩炴帴鏃舵墽琛屻€?
鎻愪氦鎴愬姛杩斿洖绀轰緥锛?
```json
{
  "id": "6c9887bdc61c43f6a7eb12653ff471a6",
  "task_id": "6c9887bdc61c43f6a7eb12653ff471a6",
  "object": "video",
  "created_at": 1779333681000,
  "status": "queued",
  "progress": 0,
  "model": "VEOomni",
  "video_url": null,
  "metadata": {
    "result_urls": []
  }
}
```

杞涓繑鍥炵ず渚嬶細

```json
{
  "task_id": "6c9887bdc61c43f6a7eb12653ff471a6",
  "status": "running",
  "state": "running",
  "task_status": "running",
  "progress": 42,
  "success": false,
  "final": false,
  "video_url": null,
  "metadata": {
    "result_urls": []
  }
}
```

瑙嗛瀹屾垚杩斿洖绀轰緥锛?
```json
{
  "task_id": "6c9887bdc61c43f6a7eb12653ff471a6",
  "status": "completed",
  "state": "completed",
  "task_status": "completed",
  "progress": 100,
  "success": true,
  "final": true,
  "video_url": "https://...",
  "url": "https://...",
  "metadata": {
    "result_urls": ["https://..."]
  }
}
```

鍥剧墖瀹屾垚杩斿洖绀轰緥锛?
```json
{
  "task_id": "9cb1affd6b014eb1b1fa3903f3a5dec6",
  "status": "completed",
  "progress": 100,
  "success": true,
  "final": true,
  "image_url": "https://...",
  "url": "https://...",
  "metadata": {
    "result_urls": ["https://..."]
  }
}
```

鍙栫粨鏋滄椂寤鸿鎸夎繖涓紭鍏堢骇璇诲彇锛?
```text
url
video_url
image_url
metadata.result_urls[0]
```

## 3. 妯″瀷閰嶇疆鎬昏

| Flow 妯″瀷 | 璇锋眰 `model` | 绫诲瀷 | `duration` | 甯哥敤姣斾緥 | 鍙傝€冨浘 |
|---|---|---|---|---|---|
| VEOomni | `VEOomni` | 瑙嗛 | `4` / `6` / `8` / `10`锛屼笉濉粯璁?`8` | `16:9` / `9:16` | 鏀寔锛屾渶澶?3 寮?|
| Banana 2 | `nana-banana-2` | 鍥剧墖 | 涓嶅～ | `1:1` / `4:3` / `3:4` / `16:9` / `9:16` | 鏀寔锛屾渶澶?10 寮?|
| Banana Pro | `nana-banana-pro` | 鍥剧墖 | 涓嶅～ | `1:1` / `4:3` / `3:4` / `16:9` / `9:16` | 鏀寔锛屾渶澶?10 寮?|
| Veo 3.1 Lite | `veo-3-1-lite` | 瑙嗛 | 涓嶅～榛樿 `8`锛屽～鍐欐椂蹇呴』鏄?`8` | `16:9` / `9:16` | 鏀寔锛岄灏惧抚 1-2 寮狅紝鎴栧鍙傝€冨浘鏈€澶?3 寮?|
| Veo 3.1 Fast | `veo-3-1-fast` | 瑙嗛 | 涓嶅～榛樿 `8`锛屽～鍐欐椂蹇呴』鏄?`8` | `16:9` / `9:16` | 鏀寔锛岄灏惧抚 1-2 寮狅紝鎴栧鍙傝€冨浘鏈€澶?3 寮?|
| Veo 3.1 Quality | `veo-3-1-quality` | 瑙嗛 | 涓嶅～榛樿 `8`锛屽～鍐欐椂蹇呴』鏄?`8` | `16:9` / `9:16` | 鏀寔锛岄灏惧抚 1-2 寮狅紝鎴栧鍙傝€冨浘鏈€澶?3 寮?|
| Veo 3.1 Fast 鍏煎鍚?| `veo-3-1` | 瑙嗛 | 涓嶅～榛樿 `8`锛屽～鍐欐椂蹇呴』鏄?`8` | `16:9` / `9:16` | 绛夊悓浜?`veo-3-1-fast` |

## 4. 鍙傛暟璇存槑

| 鍙傛暟 | 绫诲瀷 | 蹇呭～ | 閫傜敤妯″瀷 | 璇存槑 |
|---|---|---|---|---|
| `model` | string | 鏄?| 鍏ㄩ儴 | 濉?`VEOomni`銆乣nana-banana-2`銆乣nana-banana-pro`銆乣veo-3-1-lite`銆乣veo-3-1-fast`銆乣veo-3-1-quality` |
| `prompt` | string | 鏄?| 鍏ㄩ儴 | 鐢熸垚鎻愮ず璇?|
| `aspect_ratio` | string | 鍚?| 鍏ㄩ儴 | 甯哥敤 `16:9`銆乣9:16`锛涘浘鐗囦篃鏀寔 `1:1`銆乣4:3`銆乣3:4` |
| `duration` | number | 瑙嗛寤鸿濉?| 瑙嗛 | `VEOomni` 鏀寔 `4/6/8/10`锛沄eo 3.1 Lite/Fast/Quality 褰撳墠鎺ュ叆鍥哄畾鐢?`8`锛屼笉濉粯璁?`8` |
| `images` | array | 鍚?| 鍏ㄩ儴 | 参考图数组。`VEOomni` 默认按多参考图处理，最多 3 张；普通 Veo 3.1 首尾帧按顺序放 `[首帧, 尾帧]` |
| `Ingredients_images` | array | 鍚?| 瑙嗛 | 澶氬弬鑰冨浘瑙嗛锛屾渶澶?3 寮狅紝閫傚悎鍟嗗搧銆佷汉鐗┿€佸満鏅鍙傝€冨悎鎴?|
| `first_image_url` | string | 鍚?| 瑙嗛/鍥剧墖 | 鍗曞紶棣栧浘鎴栧弬鑰冨浘 |
| `image_url` | string | 鍚?| 瑙嗛/鍥剧墖 | 鍗曞紶棣栧浘鎴栧弬鑰冨浘锛屽拰 `first_image_url` 绫讳技 |
| `last_image_url` | string | 鍚?| Veo 3.1 瑙嗛 | 灏惧抚鍥撅紝闇€瑕佸悓鏃舵彁渚涢鍥?|
| `end_image_url` | string | 鍚?| Veo 3.1 瑙嗛 | 灏惧抚鍥撅紝鍜?`last_image_url` 绫讳技 |
| `resolution` | string | 鍚?| Banana 鍥剧墖 | 鍙～ `1k` 鎴?`2k` |
| `seconds` | number | 鍚?| 瑙嗛 | `duration` 鐨勫吋瀹瑰瓧娈碉紝寤鸿浼樺厛鐢?`duration` |
| `ratio` / `size_ratio` | string | 鍚?| 鍏ㄩ儴 | `aspect_ratio` 鐨勫吋瀹瑰瓧娈碉紝寤鸿浼樺厛鐢?`aspect_ratio` |

鍙傝€冨浘鍙互鐩存帴鍐欏瓧绗︿覆锛?
```json
{
  "images": [
    "https://your-domain.com/ref1.jpg",
    "https://your-domain.com/ref2.jpg"
  ]
}
```

涔熷彲浠ュ啓瀵硅薄锛?
```json
{
  "images": [
    { "url": "https://your-domain.com/ref1.jpg" },
    { "image_url": "https://your-domain.com/ref2.jpg" }
  ]
}
```

## 5. VEOomni 鎺ュ叆绀轰緥

### 5.1 鏂囩敓瑙嗛

```bash
curl -X POST "http://103.218.243.87:8080/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "VEOomni",
    "prompt": "a cinematic product reveal, smooth camera movement, premium lighting",
    "duration": 4,
    "aspect_ratio": "16:9"
  }'
```

### 5.2 鍙傝€冨浘鐢熸垚瑙嗛


VEOomni 是多参考图模型：`images` 默认按多参考图处理，最多 3 张。
如果要强制走首帧/尾帧 I2V，请额外传 `video_mode: "i2v"`，此时 `images` 最多 2 张，顺序为 `[首帧, 尾帧]`。
```bash
curl -X POST "http://103.218.243.87:8080/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "VEOomni",
    "prompt": "turn this reference into a cinematic short video with gentle motion",
    "duration": 4,
    "aspect_ratio": "16:9",
    "images": [
      "https://your-domain.com/ref-1.jpg",
      "https://your-domain.com/ref-2.jpg",
      "https://your-domain.com/ref-3.jpg"
    ]
  }'
```

### 5.3 澶氬弬鑰冨浘鐢熸垚瑙嗛

```json
{
  "model": "VEOomni",
  "prompt": "combine these references into a polished product video",
  "duration": 6,
  "aspect_ratio": "9:16",
  "images": [
    "https://your-domain.com/product.jpg",
    "https://your-domain.com/background.jpg",
    "https://your-domain.com/style.jpg"
  ]
}
```

## 6. nana-banana-2 鎺ュ叆绀轰緥

### 6.1 鏂囩敓鍥?
```bash
curl -X POST "http://103.218.243.87:8080/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nana-banana-2",
    "prompt": "a cute banana mascot wearing sunglasses, studio lighting",
    "aspect_ratio": "1:1",
    "resolution": "1k"
  }'
```

### 6.2 鍙傝€冨浘鐢熷浘

```json
{
  "model": "nana-banana-2",
  "prompt": "make a clean commercial poster using the reference image",
  "aspect_ratio": "4:3",
  "resolution": "1k",
  "images": [
    "https://your-domain.com/ref.jpg"
  ]
}
```

## 7. nana-banana-pro 鎺ュ叆绀轰緥

### 7.1 鏂囩敓鍥?
```bash
curl -X POST "http://103.218.243.87:8080/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nana-banana-pro",
    "prompt": "premium editorial product photo, luxury magazine style",
    "aspect_ratio": "16:9",
    "resolution": "2k"
  }'
```

### 7.2 鍙傝€冨浘鐢熷浘

```json
{
  "model": "nana-banana-pro",
  "prompt": "create a high-end product image using the references",
  "aspect_ratio": "1:1",
  "resolution": "2k",
  "images": [
    "https://your-domain.com/ref1.jpg",
    "https://your-domain.com/ref2.jpg"
  ]
}
```

## 8. Veo 3.1 Lite / Fast / Quality 鎺ュ叆绀轰緥

### 8.1 鏂囩敓瑙嗛

鍙敤妯″瀷鍚嶏細

```text
veo-3-1-lite
veo-3-1-fast
veo-3-1-quality
```

鍏煎鏃у啓娉曪細`veo-3-1` 绛夊悓浜?`veo-3-1-fast`銆?
Veo 3.1 褰撳墠瀵瑰鎺ュ叆鍥哄畾浣跨敤 `duration: 8`锛涗笉濉椂鏈嶅姟绔篃浼氭寜 `8` 澶勭悊銆?
```bash
curl -X POST "http://103.218.243.87:8080/v1/videos" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3-1-quality",
    "prompt": "a cinematic drone shot over a futuristic city at sunrise",
    "duration": 8,
    "aspect_ratio": "16:9"
  }'
```

### 8.2 鍗曢鍥惧浘鐢熻棰?
```json
{
  "model": "veo-3-1-lite",
  "prompt": "animate the subject with smooth camera movement and natural motion",
  "duration": 8,
  "aspect_ratio": "9:16",
  "first_image_url": "https://your-domain.com/first.jpg"
}
```

### 8.3 棣栧熬甯ц棰?
`images` 鏀?2 寮犲浘鏃讹紝椤哄簭鏄?`[棣栧抚, 灏惧抚]`銆?
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

涔熷彲浠ヨ繖鏍峰啓锛?
```json
{
  "model": "veo-3-1-fast",
  "prompt": "animate smoothly from the first frame to the last frame",
  "duration": 8,
  "aspect_ratio": "9:16",
  "first_image_url": "https://your-domain.com/first.jpg",
  "last_image_url": "https://your-domain.com/last.jpg"
}
```

### 8.4 澶氬弬鑰冨浘瑙嗛

澶氬弬鑰冨浘寤鸿鐢?`Ingredients_images`锛屾渶澶?3 寮犮€?
```json
{
  "model": "veo-3-1-quality",
  "prompt": "combine these references into a cinematic product launch video",
  "duration": 8,
  "aspect_ratio": "16:9",
  "Ingredients_images": [
    "https://your-domain.com/product.jpg",
    "https://your-domain.com/person.jpg",
    "https://your-domain.com/background.jpg"
  ]
}
```

## 9. JavaScript 寮傛杞绀轰緥

```js
const BASE_URL = "http://103.218.243.87:8080";
const API_KEY = "YOUR_API_KEY";

async function createFlowTask(payload) {
  const res = await fetch(`${BASE_URL}/v1/videos`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (!res.ok) {
    throw new Error(`create failed: ${res.status} ${await res.text()}`);
  }

  const data = await res.json();
  return data.task_id || data.id;
}

async function pollFlowTask(taskId) {
  while (true) {
    const res = await fetch(`${BASE_URL}/v1/videos/${taskId}`, {
      headers: {
        Authorization: `Bearer ${API_KEY}`
      }
    });

    if (!res.ok) {
      throw new Error(`poll failed: ${res.status} ${await res.text()}`);
    }

    const data = await res.json();
    const status = data.status || data.state || data.task_status;

    if (status === "completed") {
      return data.url || data.video_url || data.image_url || data.metadata?.result_urls?.[0];
    }

    if (status === "failed") {
      const message = data.error?.message || data.error_message || "task failed";
      throw new Error(message);
    }

    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
}

async function main() {
  const taskId = await createFlowTask({
    model: "VEOomni",
    prompt: "a cinematic product reveal, smooth camera movement",
    duration: 4,
    aspect_ratio: "16:9"
  });

  const resultUrl = await pollFlowTask(taskId);
  console.log("result:", resultUrl);
}

main().catch(console.error);
```

## 10. Python 寮傛杞绀轰緥

```python
import time
import requests

BASE_URL = "http://103.218.243.87:8080"
API_KEY = "YOUR_API_KEY"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def create_flow_task(payload):
    resp = requests.post(f"{BASE_URL}/v1/videos", headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("task_id") or data.get("id")


def poll_flow_task(task_id):
    while True:
        resp = requests.get(
            f"{BASE_URL}/v1/videos/{task_id}",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status") or data.get("state") or data.get("task_status")

        if status == "completed":
            return (
                data.get("url")
                or data.get("video_url")
                or data.get("image_url")
                or (data.get("metadata") or {}).get("result_urls", [None])[0]
            )

        if status == "failed":
            error = data.get("error") or {}
            raise RuntimeError(error.get("message") or data.get("error_message") or "task failed")

        time.sleep(5)


if __name__ == "__main__":
    task_id = create_flow_task({
        "model": "veo-3-1-fast",
        "prompt": "a cinematic drone shot over a futuristic city at sunrise",
        "duration": 8,
        "aspect_ratio": "16:9",
    })
    print(poll_flow_task(task_id))
```

## 11. 甯歌閿欒澶勭悊

### 11.1 浠诲姟澶辫触

澶辫触鏃朵竴鑸細杩斿洖锛?
```json
{
  "status": "failed",
  "progress": 98,
  "error": {
    "message": "閿欒璇存槑",
    "code": "閿欒浠ｇ爜"
  }
}
```

鍓嶇涓嶈鍙垽鏂?HTTP 鐘舵€佺爜銆傜敓鎴愭帴鍙ｆ彁浜ゆ垚鍔熷悗锛屽悗缁け璐ヤ細浣撶幇鍦ㄨ疆璇㈢粨鏋滈噷鐨?`status: failed`銆?
### 11.2 鍙傝€冨浘琚?Flow 鎷掔粷

濡傛灉閿欒閲屽嚭鐜帮細

```text
PUBLIC_ERROR_MINOR_UPLOAD
```

閫氬父琛ㄧず鍙傝€冨浘涓婁紶琚?Flow 鎷掔粷锛屽彲鑳藉寘鍚湡浜鸿倴鍍忋€佹湭鎴愬勾浜?鍎跨涓讳綋銆佹湭鎺堟潈浜哄儚锛屾垨鍏朵粬涓嶇鍚堝浘鐗囦笂浼犳斂绛栫殑鍐呭銆傚墠绔缓璁槑纭彁绀猴細

```text
鍙傝€冨浘涓婁紶琚?Flow 鎷掔粷锛氬浘鐗囧彲鑳藉寘鍚湡浜鸿倴鍍忋€佹湭鎴愬勾浜?鍎跨涓讳綋銆佹湭鎺堟潈浜哄儚鎴栧叾浠栦笉绗﹀悎鍥剧墖涓婁紶鏀跨瓥鐨勫唴瀹广€傝鏇存崲鍥剧墖鍚庨噸璇曘€?```

### 11.3 `bad port`

娴忚鍣ㄥ墠绔笉瑕佷娇鐢?`6000` 绔彛锛岄儴鍒嗘祻瑙堝櫒浼氭妸瀹冩嫤鎴负 `bad port`銆傜幇鍦ㄤ娇鐢ㄧ殑鏄細

```text
http://103.218.243.87:8080
```

### 11.4 杞涓€鐩存病鏈夌粨鏋?
寤鸿妫€鏌ワ細

1. 鏄惁杞浜嗘纭殑 `task_id`銆?2. 鏄惁甯︿簡 `Authorization: Bearer YOUR_API_KEY`銆?3. 鏄惁浣跨敤 `GET /v1/videos/{task_id}` 鎴?`GET /v1/tasks/{task_id}`銆?4. 鏄惁鎶婃渶缁堢粨鏋滀粠 `url` / `video_url` / `image_url` / `metadata.result_urls[0]` 閲屽彇鍑恒€?
## 12. 鎺ㄨ崘鍓嶇閰嶇疆

涓昏妯″瀷鍙互杩欐牱閰嶇疆锛?
```json
[
  {
    "name": "VEOomni",
    "model": "VEOomni",
    "type": "video",
    "duration": [4, 6, 8, 10],
    "default_duration": 4,
    "aspect_ratios": ["16:9", "9:16"],
    "supports_reference_images": true,
    "max_reference_images": 3
  },
  {
    "name": "Banana 2",
    "model": "nana-banana-2",
    "type": "image",
    "aspect_ratios": ["1:1", "4:3", "3:4", "16:9", "9:16"],
    "resolution": ["1k", "2k"],
    "supports_reference_images": true,
    "max_reference_images": 10
  },
  {
    "name": "Banana Pro",
    "model": "nana-banana-pro",
    "type": "image",
    "aspect_ratios": ["1:1", "4:3", "3:4", "16:9", "9:16"],
    "resolution": ["1k", "2k"],
    "supports_reference_images": true,
    "max_reference_images": 10
  },
  {
    "name": "Veo 3.1 Lite",
    "model": "veo-3-1-lite",
    "type": "video",
    "duration": [8],
    "default_duration": 8,
    "aspect_ratios": ["16:9", "9:16"],
    "supports_reference_images": true,
    "max_reference_images": 3
  },
  {
    "name": "Veo 3.1 Fast",
    "model": "veo-3-1-fast",
    "type": "video",
    "duration": [8],
    "default_duration": 8,
    "aspect_ratios": ["16:9", "9:16"],
    "supports_reference_images": true,
    "max_reference_images": 3
  },
  {
    "name": "Veo 3.1 Quality",
    "model": "veo-3-1-quality",
    "type": "video",
    "duration": [8],
    "default_duration": 8,
    "aspect_ratios": ["16:9", "9:16"],
    "supports_reference_images": true,
    "max_reference_images": 3
  }
]
```
