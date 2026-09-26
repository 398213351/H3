# SiftQ MiniMax-H3 → OpenAI 兼容 API 网关

把 `https://siftq.com/minimax-h3/try` 的匿名试用通道（MiniMax H3 图生视频）封装成
OpenAI 风格 API。基于对页面 JS bundle 的逆向 + 实测。

## 逆向结论（怎么绕限额）

试用接口 `/api/minimax-trial/*` 没有任何 cookie / 账号鉴权，全部信任客户端自产标识：

| 标识 | 来源 | 作用 |
|---|---|---|
| `X-MiniMax-Trial-Client` 头 + `client_id` 表单字段 | 前端 `mmtrial_<uuid>`，localStorage `minimax-h3-trial-client-id-v1` | 任务归属 |
| `visitorId` 表单字段 | 前端 `mmguest_<uuid>`，localStorage `minimax-h3-telemetry-guest-visitor-id-v2` | 遥测 |
| `X-Forwarded-For` 头 | 浏览器不带，由前端/代理写入 | **服务端限流键** |

服务端限额逻辑（实测）：

- 匿名每日 2 次生成（`anonymous_daily_limit: 2`），按 **XFF 第一个值**计数；
- 超出返回 `429 rate_limit_error`；
- 换 `mmtrial_` client id **无效**（实测仍 429，usage 显示 used:2）；
- 换 UA **无效**；
- **伪造 XFF 立即重置配额**（实测 4/4 成功，`remaining` 回 1）；
- 浏览器 cookie 与试用通道完全无关（接口无 Set-Cookie，`credentials:"include"` 只是摆设）。

### 生成端点（重要）

上游有两条生成通道，行为完全不同：

| 端点 | prompt | 画幅 | 说明 |
|---|---|---|---|
| `/api/minimax-trial/video-generation` | **忽略** | 任意 | 无 showcase 时的试用口；服务端固定套用 showcase 编排 prompt（一律产出「跳舞」类内容） |
| `/api/minimax-trial/showcase/video-generation` | **生效** | **仅 9:16** | showcase 驱动口；`showcase_id` 必填但显式 `prompt` 会覆盖其编排；匿名可用 |

本网关一律走 showcase 通道：`prompt` 真实控制出片，`showcase_id` 用
`GATEWAY_SHOWCASE_ID`（默认 `case-mtqzygu8`）兜底；`prompt` 为空时注入
`GATEWAY_DEFAULT_PROMPT`（中性自然动作），避免回退到 showcase 的固定编排。

因此 **`size` 仅支持 9:16**，其他画幅返回 400。这是上游硬限制（官方前端
`sRe` 同样写死 `"9:16"`），不是网关限制。

另外两条路（未采用）：`/api/minimax-experience/admin/accounts` + `X-MiniMax-Admin-Key`
可铸 2000 积分体验账户，但 key 只在站方手里；登录走 Google Firebase idToken，
账号农场成本高。

## 运行

```bash
pip install -r requirements.txt
python gateway.py            # 默认 127.0.0.1:8787
```

### Linux 部署

```bash
unzip h3-studio-gateway.zip && cd h3-studio-gateway
./start.sh                   # 自动建 .venv 并启动

# 或 systemd 常驻（先手动 pip install -r requirements.txt 到 .venv）
sudo cp -r . /opt/h3-studio-gateway && cd /opt/h3-studio-gateway
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp h3-gateway.service /etc/systemd/system/
sudo systemctl enable --now h3-gateway
journalctl -u h3-gateway -f  # 看日志
```

对外暴露时：网关监听保持 `127.0.0.1`，前面套 nginx/Caddy 反代 + TLS，并设置
`GATEWAY_API_KEY`（所有 `/v1/*` 需 `Authorization: Bearer <key>`）。出站需能访问
`siftq.com`，国内机器通常要配 `PROXY_LIST`。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `GATEWAY_API_KEY` | 空 | 设置后所有 `/v1/*` 需要 `Authorization: Bearer <key>` |
| `GATEWAY_MAX_CONCURRENT` | `4` | 上游并发上限 5，留 1 余量 |
| `GATEWAY_SUBMIT_TIMEOUT` | `900` | 提交重试总时限（秒）；配额按伪造 IP 计且身份无限铸造，429 即换新身份直到成功，超时兜底 |
| `PROXY_LIST` | 空 | 逗号分隔的 http/socks5 代理；设置后走真实代理 IP，不再伪造 XFF |
| `GATEWAY_DB` | `./gateway.db` | SQLite 任务库 |
| `GATEWAY_VIDEOS_DIR` | `./videos` | 成片本地缓存目录；出片即落盘（上游约 1-2 天回收任务），`/content` 优先发本地 |
| `GATEWAY_UPLOADS_DIR` | `./uploads` | 输入图暂存；上游执行失败时自动换新身份重投（`GATEWAY_TASK_RESUBMITS` 次，默认 8，间隔递增） |
| `GATEWAY_SHOWCASE_ID` | `case-mtqzygu8` | showcase 通道的引用素材 id（`prompt` 会覆盖其编排） |
| `GATEWAY_DEFAULT_PROMPT` | 中性自然动作 | 调用方不传 `prompt` 时的兜底文案 |

## 用法

### Sora 风格视频接口

```bash
# JSON（image_url 或 base64 data URL）
curl -X POST http://127.0.0.1:8787/v1/videos \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-h3","image_url":"https://example.com/ref.jpg","seconds":"6","size":"9:16"}'
# => 202 {"id":"video_...","status":"queued",...}

# 或 multipart 直传文件
curl -X POST http://127.0.0.1:8787/v1/videos -F "image=@ref.jpg" -F "seconds=10"

# 轮询 / 下载
curl http://127.0.0.1:8787/v1/videos/video_xxx
curl -O http://127.0.0.1:8787/v1/videos/video_xxx/content
```

`size` **仅支持 `9:16`**（上游 showcase 通道硬限制），其他值返回 400。
`seconds` 支持 6 / 10 / 15（4–7→6，8–12→10，13–20→15）。模型 `minimax-h3-10s` /
`minimax-h3-15s` 直接锁定对应时长。`prompt` 字段真实控制出片内容（已实测：
图+「the player scores a try」产出对应动作，不再是固定跳舞片）。

### Chat Completions 垫片

给只会说 chat completions 的客户端用：最后一条 user message 里带图片
（`image_url` part，支持 http(s) / base64 data URL），返回视频下载链接。

```bash
curl -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"minimax-h3",
    "messages":[{"role":"user","content":[
      {"type":"text","text":"make it move"},
      {"type":"image_url","image_url":{"url":"https://example.com/ref.jpg"}}
    ]}],
    "video":{"seconds":"6","size":"9:16"},
    "wait_seconds":180
  }'
```

- 默认异步：立刻返回 task id + 下载链接，自己轮询 `/v1/videos/{id}`；
- `wait_seconds: N` 阻塞等到出片（或超时）；
- `stream: true` 走 SSE。

注意：出片为 9:16 竖屏（上游 showcase 通道限制）；`prompt` 生效。

### 其他

- `GET /v1/models` — 模型列表（`minimax-h3` / `minimax-h3-10s` / `minimax-h3-15s`）
- `GET /v1/trial/usage` — 轮换器状态（已铸身份数、耗尽数、代理模式）
- `GET /health`
- `GET /` — H3 片场前端（手绘拼贴风，内置于 `web/`）

## 轮换引擎

每次提交：铸造新 `mmtrial_<uuid>` + `mmguest_<uuid>` + 随机公网 XFF，每个假 IP 理论
2 次/天；429 即烧毁当前身份并铸新身份重试（时限 `GATEWAY_SUBMIT_TIMEOUT`，默认
900s，近似无限）；提交受 `MAX_CONCURRENT` 信号量控制；
后台 poller 每 3s 轮询上游直到终态，结果进 SQLite。任务查询/下载只用提交时保存的
`access_token` + `client_id` + XFF（实测 query 闸门是 access_token）。
