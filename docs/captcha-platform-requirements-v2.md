# 打码平台需求单 v2 —— px_press 真实挑战求解修复

> 发单方：grok-register outlook protocol 模式
> 日期：2026-11-08（基于 2026-11-08 凌晨 live 联调 n=3/n=4 + 独立变量隔离实验）
> 前置文档：`docs/captcha-platform-requirements.md`（v1，接口契约不变，本单只修「真挑战解不出」）
> 结论先行：**v1 验收 1/2 在合成页上已通过；真实 hsprotect 挑战在 110–120s
> 预算内连非通过级 `_px3` 都种不出来。根因不是网络/代理/脚本加载，是
> solver 直接导航 iframe URL 缺了真实嵌入上下文。**

---

## 1. 现象（live 实测，可复现）

2026-11-08 02:44–03:16（gz-cvm，slidex 与注册机同机）：

| 实验 | 任务 | 出口 | 结果 |
|---|---|---|---|
| n=4 press | 真 challengeUrl（hsprotect iframe）| 18016 住宅 | `px_token_timeout`，120.6s |
| n=4 silent 兜底 | signup.live.com 首页 | 18016 住宅 | `px_token_timeout` |
| 隔离 A | signup.live.com 静默 | 18016 | `px_token_timeout`，100.4s |
| 隔离 B | signup.live.com 静默 | 直连（无 proxy）| `px_token_timeout`，101.2s |
| 隔离 C | 真 challengeUrl press + 真 uuid | 18016 | `px_token_timeout`，110.5s |

排除项（都已验证，不用再查）：

- **代理可达性**：slidex 与 gz-cvm 同机，`127.0.0.1:18016` 对浏览器可达；
  curl 经 18016 抓 signup.live.com 与 hsprotect 域全部正常（200/302）。
- **脚本链可达性**：iframe HTML 200；`client.hsprotect.net/PXzC5j78di/main.min.js`
  200（222KB 真传感器）。
- **直连也不行** → 不是 IP 信誉问题（腾讯云直连同样种不出）。
- **slidex 内部状态机正常**：`px3 without :1000:`、`chromium gone`、
  `navigate failed` 等日志一次都没出现——即轮询期间 cookie jar 里
  **始终没有任何 PX cookie**，不是「种了但段位低」。

对照组：`deep_check.py` 的合成 pass 页（直接 `document.cookie` 写
`_px3=1:a:1000:...`）全部通过 → solver 的 cookie 抽取/判定链路没毛病。

## 2. 根因分析（我们读了 iframe 的启动脚本，事实如下）

`https://iframe.hsprotect.net/index.html?app_id=PXzC5j78di&session_id=...`
返回的 HTML 内嵌启动脚本（HUMAN Security 官方 iframe 页）做了这些事：

1. `window._pxAppId` 从 **URL query** 读（`app_id`），session_id 写
   `_pxParam1` —— 这部分 slidex 现在的做法能对上。
2. **`window._pxVid` 只有两个来源**：
   - `document.cookie` 里的 `_pxvid`（同域读）；
   - `localStorage[appId+"_pxvid"]`（base64 JSON）。
   ——iframe 页自身**不生成 vid**。
3. **captcha.js 的加载 URL 依赖 `_pxVid`**：
   `captcha.js?a=c&m=0&u=<window._pxUuid>&v=<window._pxVid>`。
   vid 为空时 captcha.js 的请求参数就是 `v=`（空）。
4. **`block`/`setToWindow`/vid/uuid 的正式下发通道是父页 → iframe 的
   `postMessage`**（`case "block": d(e.jsonResponse, e.requestUrl)`，
   `d()` 里设置 `_pxUuid/_pxVid` 再拉 captcha.js）。真实 SPA 里这个
   消息由 `signup.live.com` 的宿主页发出；**slidex 直接把 iframe URL
   当顶层页导航，没有父页，postMessage 通道永远沉默**。
5. iframe 还会拦 `document.cookie` 的 setter 把 `_pxvid/_px3/_pxde`
   `postMessage` 回父页 —— 说明**官方设计里 cookie 的落点是父页域**
   （signup.live.com），iframe 域只做传感器执行。slidex 在 iframe
   顶层页 jar 里等 `_px3`，等的东西官方流程根本不往那里放。

一句话：**真实链路里 PX token 的产生依赖「宿主页 + iframe」双层协作；
现在只有 iframe 单层，传感器要么不进入工作分支，要么把 token 发到了
不存在的父页。**

## 3. 需要你做的（按优先级）

### 3.1（P0）press 模式改为「宿主页 + 注入 iframe」双页上下文

press 求解不再直接导航 `challengeUrl`，改为：

1. 顶层页导航到 `websiteURL`（`https://signup.live.com/`），等
   domcontentloaded；
2. 在顶层页内**动态插入 iframe**（src = `challengeUrl`，宽高 ≥300×74，
   id `px-captcha` 由 iframe 内部自带）；或者等价地：顶层页导航后用
   `page.goto(challengeUrl)` 换 URL 也行——但**必须先让顶层页在
   signup.live.com 域上把会话 cookie 落好**（v1 payload 的 `cookies`
   字段继续注入到 signup.live.com 域）；
3. 宿主页与 iframe 的 postMessage 由浏览器自然打通（同无跨域限制，
   Playwright 无需额外处理，只要别禁 iframe）；
4. token 读取照旧扫**整个 context cookie jar**（你现在就是这么做的），
   但请同时扫 `.signup.live.com` 域 —— iframe 会把 cookie postMessage
   给父页，父页脚本（PX main.min.js）会写回宿主页域。

验收锚点：解出的 `_px3` 若来自 `.signup.live.com` 域也算成功
（`extract_px_tokens` 现有归一化逻辑不用改）。

### 3.2（P0）vid/uuid 缺失时不再裸导航，先补种子

现在 `uuid/vid` 为空也照常导航（隔离实验 C 传了真 uuid 也没用，因为
卡在 3.1）。改成：

- `vid` 非空 → 注入 `_pxvid` cookie 到 `.hsprotect.net` 与
  `websiteURL` 域（`_cookie_specs` 已有这个机制，确认它真的生效——
  建议加一条 debug 日志打印注入的 cookie 名与域，值脱敏）；
- `vid` 为空 → **不要沉默继续**：要么立刻 fail（
  `caller_vid_required`，让我们侧排查为什么 challengeDetails 没给
  deviceId），要么自动走一次「裸 silent」拿 `client.hsprotect.net`
  下发的初始 vid 再重试 press。两选一，倾向后者（自动补种子）。

### 3.3（P1）增加可观测性：一次任务的传感器执行链要有迹可循

当前真实挑战失败时日志只有一行 `status=fail in 110s`，无法区分
「脚本没执行」「执行了被拒」「token 发去了父页」。请在任务记录或
`GET /v2/tasks/{id}` 的 fail 响应里附 `diagnostics`（脱敏）：

- `iframe_loaded`：challengeUrl 是否 200 且含 `px-captcha` div；
- `captcha_js_loaded`：`captcha.js` 请求是否发生、状态码；
- `main_min_js_loaded`：`main.min.js` 是否加载；
- `collector_seen`：是否有发往 `collector-*.hsprotect.net`（或
  `collector-a.px-client.net`）的请求；
- `parent_message_seen`：iframe 是否收到过父页 postMessage（加
  init script 监听即可，记 type 不记内容）；
- `px_cookie_names_seen`：任务结束时 jar 里所有 `_px*` cookie 名列表
  （只有名字+域，不要值）。

字段全可选（老客户端忽略未知字段），但有了它我们三方联调就不用再猜。

### 3.4（P2）silent 模式同样受益

3.1 的双页结构对 silent 同样成立（宿主页 = websiteURL 本身）。silent
修好后，我们侧 press 失败自动降级 silent 的链路才有意义——现在
silent 也是 `px_token_timeout`，降级等于换个姿势超时。

## 4. 验收标准（v2 增量）

1. **真挑战冒烟**：我们给一个 fresh 的真 `challengeUrl`（live 风控链
   现场 `risk/initialize` 产出，5 分钟内有效），press 任务在 150s 内
   返回 success，`px3` 含 `:1000:`。
2. **诊断可见**：上述任务无论成败，fail/success 响应带 §3.3 的
   `diagnostics` 字段。
3. **vid 种子链路**：vid 为空的请求按 §3.2 的策略二自动补种子后重试，
   不再裸导航沉默超时。
4. **回归**：`deep_check.py` 全部通过（合成 pass 页行为不回退）。
5. **终验**（联合）：我们侧 live n=1 —— risk/verify #2 携带你的 token
   不再 403 riskBlock，注册链走通到 CreateAccount。

## 5. 我们侧同步做的配合

- `challengeUrl` 现在传的是 `challengeDetails.challengeUrl`（真 iframe
  URL，含 session_id）而不是只传 hsprotect 域根 —— v1 文档 §3.1 示例
  已如此，现场抓包确认过；
- `vid`：`challengeDetails.deviceId` 若为空我们会在需求侧补发（当前
  live 观察 deviceId 常为空，这正是 §3.2 要处理的情况）；
- 联调窗口照旧：你部署后给 base URL + key（或直接用现有
  `10.144.144.8:8077` 热更新），我们把 `CAPTCHA_RUN_BASE` 指过去先跑
  §4.1 冒烟，再上 live n=1。

## 6. 时间线建议

- §3.2（vid 策略）+ §3.3（诊断）半天级；
- §3.1（双页上下文）一天级（涉及 solver 导航流重构）；
- 全部就绪后我们 24h 内可回归 live n=1 → n=2。
