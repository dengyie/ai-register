# 打码平台需求单（给 slidex）

> 发单方：grok-register outlook protocol 模式
> 日期：2026-11-07（含当天 live 逆向定因的最新协议事实）
> 你要建的东西：一个**自建 PX (PerimeterX) press-token 求解服务**，替代
> captcha.run。本文把协议事实、接口契约、验收标准一次说全，照做即可。

---

## 1. 目标一句话

给定 Outlook 注册风控链的挑战上下文，返回一组能让
`signup.live.com` 的 `risk/verify` 接受的 **PX token 四件套
（px3 / pxde / pxvid / pxcts）**。核心工作是驱动
`https://iframe.hsprotect.net` 的 PX JS（appId `PXzC5j78di`），
让它对指定 session 产出通过级 token。

## 2. 为什么必须自建（背景 30 秒）

- 纯 HTTP 协议流无法产出 PX token：`risk/initialize` 返回的
  `humanSensorUrl`（hsprotect iframe）需要真实 JS 执行环境才能生成
  `_px3/_pxde/_pxvid` cookie 与 beacon。没有 token，`risk/verify`
  稳定返回 `403 AADSTS7005106 riskBlock`（2026-11-07 live 实测）。
- captcha.run 是外部付费服务，key/价格/可用性都不可控 → 自建。

## 3. 输入输出契约（你的 API 必须长这样）

### 3.1 创建任务

`POST /v2/tasks/`，Header `Authorization: Bearer <key>`，JSON body：

```json
{
  "taskId": "<你生成，幂等键>",
  "captchaType": "press",
  "appId": "PXzC5j78di",
  "uaid": "<ServerData.sUnauthSessionID>",
  "uuid": "<challengeDetails.leadingChallengeId，可空>",
  "vid": "<challengeDetails.deviceId，可空>",
  "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
  "country": "US",
  "timezone": "America/New_York",
  "websiteURL": "https://signup.live.com/",
  "challengeUrl": "<humanSensorUrl 或 challengeDetails.challengeUrl>",
  "cookies": {"<name>": "<value>", "...": "..."},
  "proxy": {
    "host": "x.x.x.x", "port": 12345,
    "login": "user", "password": "pass"
  }
}
```

响应：`{"taskId": "..."}`（HTTP 200；失败 4xx/5xx + `{"error": "..."}`）。

### 3.2 轮询

`GET /v2/tasks/{taskId}?captchaType=press`（同 Bearer key）。

- 进行中：`{"status": "processing"}`
- 成功：`{"status": "success", "response": {"px3": "...", "pxde": "...", "pxvid": "...", "pxcts": "..."}}`
  - 也接受打包格式 `{"response": {"pressToken": "<单串 packed>"}}`
    —— packed 串里需可解出四件套；解不出就按四件套逐字段给。
- 失败：`{"status": "fail", "error": "..."}`（fail/failed/error 都算失败）
- 客户端轮询节奏：press 3s×最长 180s，silent 2s×最长 90s。你的服务
  内部求解预算 ≤150s，超时应返回 fail 而不是让客户端干等。

### 3.3 成功判据（重要）

`px3` 值必须形如 `...:1000:...`（含 `:1000:` 段）——这是 PX「通过级」
token 的标志。返回不含 `:1000:` 的 px3 等于没解过，会被 risk/verify 拒绝。

## 4. 铁律：出口 IP 一致性（违反 = 全部作废）

**solver 的一切对外请求（加载 PX JS、上报 beacon、领挑战）必须走任务
payload 里给的同一个 sticky proxy**。原因：

- MSA 风控校验 PX collector 上报出口与 risk/verify 提交出口是否同 IP；
- 我们注册侧全程钉一个 sticky 出口（一个账号一个会话）；
- 你在 A 出口解 token、我们在 B 出口提交 verify → 直接 riskBlock，
  token 白产，还烧我们一个 IP。

proxy 字段给了 host/port/login/password 就必须用（HTTP 代理，支持
CONNECT）。字段为空时（我们本地测试场景）才允许直连。

## 5. 实现路径建议（供参考，不限定）

两种可行架构，任选或混合：

1. **headless 浏览器农场（推荐起步）**：Playwright/Chromium + 代理注入，
   打开 `challengeUrl`（hsprotect iframe 页），等 PX JS 自行产 cookie，
   从浏览器 cookie jar 抽 `_px3/_pxde/_pxvid/_pxcts`，组装四件套返回。
   注意 UA/时区/语言要按 payload 对齐（country/timezone/userAgent）。
2. **wasm/JS 逆向求解**：PX beacon 本地构造 + 指纹伪造。成本高、易随
   PX 版本失效，作为后期优化。

先做 (1) 打通闭环，跑量后再考虑 (2)。

## 6. 非功能需求

- **并发**：起步 ≥5 并发任务（对应 ANTIBAN 纪律 1–2 并发注册 + 重试余量）；
  目标 20。
- **不落盘**：不得持久化存储我们的 cookies/上下文（含 MSA 会话片段），
  任务完成即弃；日志脱敏（不打印完整 cookie/token 值）。
- **可观测**：`GET /health` 返回 `{ok, active_tasks, done_24h, fail_24h}`。
- **失败语义**：可重试类（网络/超时）与确定性失败（IP 被拉黑、appId
  校验失败）区分开；后者必须立刻 fail 并在 error 里写明类别。
- **部署**：内网可达即可（我们跑在本地/gz-cvm），HTTP + Bearer key，
  key 由环境变量 `CAPTCHA_RUN_KEY` 我们侧注入（兼容现有代码，无需改
  调用方）——你的服务只需兼容 captcha.run 的 `POST /v2/tasks/` +
  `GET /v2/tasks/{id}` 这两个端点的请求/响应 schema，我们把
  `CAPTCHA_RUN_BASE` 指到你的地址即可无缝切换。

## 7. 验收标准（按顺序）

1. `POST /v2/tasks/` 创建任务返回 taskId；轮询至 success 返回四件套。
2. `px3` 含 `:1000:`。
3. 拿你返回的 token，我们侧 `risk/verify #2`（challengeSolution=
   `{challengeType:"HumanCaptcha",px3,pxde,pxvid}` + riskProviderMetadata
   数组）不再 403，state 进入 continue/verified。
4. 全链路 live n=1：注册 → artifact 落盘 → 读信自检通过。
5. 并发 5 任务无串号（每个任务用各给的 proxy，出口 IP 采样一致）。

## 8. 我们侧已就绪的部分（你不用管）

- 风控链编排：initialize →（挑战时）solver 调用 → verify 提交；
- challengeSolution/riskProviderMetadata 的 SPA 精确形状（2026-11-07
  已 live 逆向定因并修正：solution 是对象、metadata 是数组、签名是
  SR 对象、birthdate 是 DD:MM:YYYY）；
- sticky 代理供给与出口一致性校验；
- 失败 burn/冷却与账号节奏控制。

## 9. 联系与联调

- 联调窗口：服务可跑后给我们 base URL + key，我们先把
  `CAPTCHA_RUN_BASE` 指过去，跑 1 个任务走通 §7.1–7.3，再上 live n=1。
- 有协议事实疑问看 `docs/outlook-protocol-notes.md`（本仓），§1–§2 与
  本单一致，以本单更新时间戳为准。
