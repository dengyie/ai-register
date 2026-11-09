# §4.1 冒烟报告（2026-11-08 05:22–05:37，gz-cvm）

> 发单方：grok-register → slidex
> **终态（2026-11-08 08:00 追记）**：slidex 传感器层修复（06:12 部署）后
> §4.1 真 challengeUrl 冒烟 **SUCCESS**（px3 含 `:1000:` pass 分段，
> `px: vid seeded ok`）；live n=5 press token 通过 risk/verify #2
> （`risk_verify_press` + `risk_verify_silent` 双步完成）。§4.5 的
> CreateAccount 环节卡在 **1312 field=phoneNumber**（风险引擎手机验证
> 闸门，非打码/协议缺陷，定性证据见 vault §12.4 与
> outlook-protocol-notes.md step 11）——打码平台侧验收全部通过，
> 剩余闸门在风控政策层。

## 1. 冒烟结果一览

| # | 任务 | 输入要点 | 终态 | 诊断关键字段 |
|---|---|---|---|---|
| S1 | press, 真 challengeUrl（live risk/initialize 现场 5min 内产出，session_id=80ec28fe） | uuid/deviceId 均空，18016 住宅出口 | fail 25.5s `caller_vid_required`（§3.2 生效：不再裸导航沉默超时）| `vid_seeded:false` |
| S1-seed | S1 内部 auto-seed（裸 silent on signup.live.com） | 25s 预算 | seed fail：jar 内无 `_pxvid` | — |
| S2 | press, 真 challengeUrl（session_id=347cde6b） | 同上，fresh 会话 | 同 S1 | 同上 |
| S3 | silent, websiteURL=signup.live.com | 18016 出口，120s | fail 121.7s `px_token_timeout` | **`iframe_loaded:true, main_min_js_loaded:true(200), captcha_js_loaded:false, collector_seen:false, parent_message_seen:false, px_cookie_names_seen:[]`** |

S1/S2 失败语义符合需求单 v2 §3.2 —— vid 为空时 fail-fast + 指明
类别，这个验收点（§4.3）算通过；但暴露了 auto-seed 拿不到 vid。

## 2. S3 诊断逐层判读

链路期望：宿主页加载 → main.min.js 执行 → POST
`collector-PXzC5j78di.hsprotect.net/api/v2/collector`（领初始 vid +
写 `_pxvid/_px3`）→ 后续 beacon。实测断在第二环：

- ✅ 宿主/挑战文档 200（`iframe_loaded`）
- ✅ main.min.js 200 加载（`main_min_js_loaded`，我们 curl 侧也复核 222KB 真传感器）
- ❌ **零 collector 请求**（`collector_seen=false`）
- ❌ 零 PX cookie（连 `_pxvid` 都没有 → S1 的 auto-seed 因此必败）

### 排除项（本轮已验证，不用重查）

- **网络**：curl 经 18016 POST collector 主机返回 415（DNS/TLS/路由全通，
  415 是"没带正确 payload"的正常拒绝，主机可达）。
- **脚本源**：`client.hsprotect.net/main.min.js` 200（沙箱内 curl 同样复核）。
- **同机代理**：slidex 与 18016 同机，浏览器走 proxy 上下文（S3 走的是
  silent 单页，proxy 在 context 上生效 —— 之前 deep_check C1 验证过）。

### 剩余唯一分支：main.min.js 在 headless Chromium 里执行后未进入上报

PX 传感器自带环境检测，静默放弃上报的常见触发：

1. **headless/自动化特征被判**（CDP 泄露、`navigator.webdriver`、
   permissions/plugins 形状等）——`_STEALTH_INIT_SCRIPT` 只补了 5 个
   属性，对现在的 PX 版本可能不够；
2. **API 缺失**：1.9G 小机器 + headless 下某些 WebAPI（蓝牙/存储配额/
   WebGPU 等）探测异常导致传感器提前退出；
3. **时钟/时序**：传感器内部分支等待某个事件（如 `focus`/可见性），
   headless 永不触发。

## 3. 请 slidex 侧做的（增量小，诊断框架已就位）

1. **P0**：在 `_solve_once` 里给页面挂 `page.on("console")` +
   `page.on("pageerror")`，main.min.js 执行期间把 console/error 记进
   `diagnostics.sensor_console`（脱敏、截断）。PX 传感器静默退出通常
   会在 console 留提示（"App communication failure" / api blocked）。
2. **P0**：diagnostics 增加 `collector_request_error`：即便 collector_seen
   为 false，也要区分「请求没发出」vs「发出了但失败」（requestfailed
   事件里有 failure text）。一个 bool + 一行 reason 就够。
3. **P1**：对比实验按钮——`SLIDEX_PX_HEADLESS=false`（有头）跑一次 S3
   同任务。若 headed 能种出 `_pxvid`，直接锁定 headless 检测；we 侧
   需求单 v2 §3.3 的 `parent_message_seen` 等字段已能支撑这个对比。
4. **P1**：stealth 加固候选（按上面对比实验结果决定）：CDP runtime
   泄露修补、`chrome.runtime` 更完整形状、`Notification.permission`
   / `permissions.query` 行为、WebGL vendor 形状。

## 4. 我们侧状态（配合项，都已就绪）

- fresh challengeUrl 生产线：live risk/initialize → hsprotect iframe URL
  （含 session_id），S1/S2 已验证 5 分钟新鲜度窗口足够；
- deviceId：live 观察当前恒为空（`challengeDetails.deviceId` 不下发），
  所以 §3.2 auto-seed 是必经路径 —— S1 的 seed fail 就是卡在 S3 同一
  断层，**collector 通了 seed 自然通**；
- risk/verify #2 提交与 `:1000:` 判定照旧（v1 §7.3）。

## 5. 验收回看

- ✅ §4.2 诊断字段（本轮全靠它定位，价值已兑现）
- ✅ §4.3 vid 缺失 fail-fast 语义（不再 120s 沉默超时）
- ⏳ §4.1 真挑战 `:1000:` 冒烟 —— 卡在传感器不上报，等 §3 P0/P1
- ⏳ §4.5 live n=1 联合验证
