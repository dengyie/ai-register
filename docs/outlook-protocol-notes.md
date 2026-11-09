# Outlook MSA 注册协议事实档案（research-then-rewrite 纪律）

> **来源与纪律**：本文是纯协议**事实**档案——端点、字段名、流程顺序、常量——
> 提炼自对 `lxf746/outlook-auto-register`（GPL-3.0）的源码级研究（只读研究，
> 未拷贝任何代码；实现见 `providers/outlook/protocol/`，本仓保持 MIT）。
> 所有常量为微软公开端点的协议事实（client_id 是 Thunderbird 等公共客户端的
> well-known 值），不构成该仓的创造性表达。
>
> **红线**：默认**禁止 skip proofs**（不绑恢复邮箱 = 高封号率）；新号冷启动
> 纪律见 `docs/ANTIBAN.md`。

## 0. 常量表（协议事实）

### 标识符

| 常量 | 值 | 用途 |
|---|---|---|
| OUTLOOK_CLIENT_ID | `9199bf20-a13f-4107-85dc-02114787ef48` | 注册 OAuth PKCE 客户端 |
| COBRAND_ID | `ab0455a0-8d03-46b9-b18b-df2f57b9e44c` | oauth20_authorize 的 cobrandid |
| SIGNUP_CLIENT_ID | `00000000487A244A` | risk verify 的 siteId/appId |
| THUNDERBIRD_CLIENT_ID | `9e5f94bc-e8a4-4e73-b8be-63364c29d753` | 邮件令牌 / login token#2（本仓 `outlook_oauth.py` 已有同名常量 PUBLIC_CLIENT_ID） |
| MSA_TENANT_ID | `9188040d-6c67-4c5b-b112-36a304b66dad` | risk API tenant 路径段（consumers 租户） |
| PX_APP_ID | `PXzC5j78di` | PerimeterX app id → collector-pxzc5j78di.hsprotect.net |
| ARKOSE_PUBLIC_KEY | `B7D8911C-5CC8-A9A3-35B0-554ACEE604DA` | FunCaptcha（浏览器路径已用） |

### 端点

| 端点 | URL |
|---|---|
| SIGNUP_API_BASE | `https://signup.live.com/API` |
| LOGIN_BASE | `https://login.live.com` |
| LOGIN_MS_BASE | `https://login.microsoftonline.com` |
| ACCOUNT_BASE | `https://account.microsoft.com` |
| PX_COLLECTOR | `https://collector-pxzc5j78di.hsprotect.net` |
| MS_TOKEN_URL | `https://login.microsoftonline.com/common/oauth2/v2.0/token`（本仓 outlook_oauth.TOKEN_URL 一致） |
| risk initialize | `https://login.microsoftonline.com/consumers/oauth2/v2.0/…/risk/initialize`（实际 URL 从 ServerData 的 `urlRiskInitialize` 动态取，路径形如 `/api/v1.0/emit/tenant/{tenant}/risk/initialize`） |
| risk verify | 同上 `risk/verify`（从 `urlRiskVerify` 取） |
| signup 入口 | `https://signup.live.com/signup?lic=1&mkt={mkt}` 或 z_style：`GET https://account.microsoft.com/` → `areaConfig.createAccountUrl` |

### 邮件 scope（逐条事实）

| 模式 | scope |
|---|---|
| graph | `https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/User.Read offline_access openid profile` |
| outlook_rest | `https://outlook.office.com/Mail.ReadWrite https://outlook.office.com/Mail.Send offline_access openid profile` |
| imap / login_exe（Thunderbird） | `https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/POP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access` |
| login_exe 全集 | imap 集合 + `https://outlook.office.com/EWS.AccessAsUser.All` + `Mail.ReadWrite` + `Mail.Send` |
| outlook web bootstrap | `https://outlook.office.com/.default offline_access openid profile` |
| login token#2 | `openid profile offline_access https://graph.microsoft.com/User.Read` |

### 国家 → mkt/lc（联动 timezone，三者必须与出口 IP 一致）

US=(EN-US,1033)、GB=(EN-GB,2057)、DE=(DE-DE,1031)、FR=(FR-FR,1036)、
JP=(JA-JP,1041)、KR=(KO-KR,1042)、SG=(EN-SG,18441)、HK=(ZH-HK,3076)、
TW=(ZH-TW,1028)、CN=(ZH-CN,2052)；默认 EN-US/1033。
timezone 映射：US→America/New_York、GB→Europe/London、JP→Asia/Tokyo、
SG→Asia/Singapore、HK→Asia/Hong_Kong…（未知回退 America/New_York）。

域名：`@outlook.com`、`@hotmail.com` + 33 个地区域（.co.uk/.de/.fr/.co.jp/…）。

## 1. 注册流程（18 步顺序事实）

z_style（默认路由）流程顺序——**只记顺序与字段名，实现重写**：

```
 1 GET  account.microsoft.com/                          → areaConfig JSON 取 createAccountUrl
 2 GET  {createAccountUrl}                              → HTML 内嵌 ServerData（raw_decode "var ServerData= "）
                                                          关键键: sUnauthSessionID(=uaid), apiCanary,
                                                          urlRiskInitialize, urlRiskVerify, oCaptchaInfo.urlDfp,
                                                          sHumanAppId, sSiteId, hpgid, iScenarioId, sru；
                                                          **终态 URL 记 signup_page_url，其 query（cobrandid/uaid/
                                                          lic/mkt…）必须原样拼到 CheckAvailable 与 CreateAccount
                                                          端点后**（z_style 事实，上游 api 端点全带页 query）
 3 GET  {urlDfp} / urlHumanIframe                        → PX 指纹预热
 4 POST {PX_COLLECTOR}/api/v2/mst/beacon                → body 空 payload {"payload": ""}（form 编码）
 5 POST {PX_COLLECTOR}/assets/js/bundle                 → 同上（间隔 ~0.3s）；Origin/Referer= https://iframe.hsprotect.net
 6 POST {SIGNUP_API}/CheckAvailableSignInNames          → body: includeSuggestions, **signInName（驼峰！
                                                          snake_case sign_inName 会拿回 HTML errcode=1046
                                                          错误页而非 JSON——2026-11-07 live 8×复现+消融定因）**,
                                                          uiflvr=1001, scid, uaid, hpgid
                                                          URL 用 ServerData.urlCheckAvailableSigninNames
                                                          （= /API/CheckAvailableSigninNames，大小写如此）；
                                                          headers: canary（缺失 → JSON error 500）
                                                          响应更新 canary；失败取 server suggestions 复验
 7 POST {urlRiskInitialize}                             → body {continuationToken}；响应取
                                                          riskInitializationData[0].humanSensorUrl
                                                          （PX JS iframe，纯 HTTP 无法产出 _px3——
                                                          silent 清除必须靠打码平台）；
                                                          **initialize 只能调一次**，同一 continuationToken
                                                          二次 initialize → AADSTS55200 invalid continuation
 8 POST {SIGNUP_API}/EvaluateExperimentAssignments      → body clientExperiments；取回 apiCanary/telemetryContext
                                                          （clientExperiments = [{name, assignmentId, treatments}]
                                                          数组，SPA 内嵌常量；best-effort——失败不阻断主链，
                                                          只丢 canary/telemetry 刷新）；
                                                          **signup API 风险头**（z_style 每个注册 API 请求都带）：
                                                          correlationId=uaid、client-request-id=uaid、
                                                          hpgid=str(ServerData.hpgid)、hpgact="0"、
                                                          Origin/Referer=signup 页 URL、canary
 9 POST {urlRiskVerify}  #1 silent                      → body: continuationToken, msaRiskVerifySignature
                                                          （**SR 对象**——必须是嵌套 JSON 对象，字符串会
                                                          "Failed to parse request body" 400；字段: memberName,
                                                          siteId=clientId||sSiteId, uiFlavor=枚举名 "Web"
                                                          （iUiFlavor=1001 的枚举名，非数字）, appId,
                                                          **birthdate="$N" 格式 "DD:MM:YYYY"**（日/月补零年最后，
                                                          非 ISO-8601）, firstName/lastName, countryCode,
                                                          verificationCode="", deviceDetails={isRdm:false},
                                                          action="SignUp"）；z_style clean 路径改发
                                                          **msaCreateSignature**（同字段但无 action、
                                                          siteId=sSiteId||"292666"、appId=""），配
                                                          riskProviderMetadata silent 数组（下条）；
                                                          riskProviderMetadata 是
                                                          [{riskProvider:"Human",px3,pxde,pxvid}] 数组——
                                                          **silent 元数据无 pxcts**（pxcts 只出现在 press
                                                          challengeSolution 双写）；纯 HTTP
                                                          无 PX token 时省略，挑战分支补
                                                          判定: state=riskChallengeRequired → 走挑战分支；
                                                          continue/verified/NO_ERROR → 放行；
                                                          403 AADSTS7005106 innerError.riskBlock → RiskBlocked
                                                          （live: 无 PX token 的 silent verify 稳定 403，
                                                          风控语义而非协议错误）
                                                          **PX token 双通道**：silent/press token 既要进 verify
                                                          body 也要进会话 cookie（`_px3/_pxde/_pxvid/pxcts` 撒
                                                          .live.com/.microsoftonline.com/.hsprotect.net 三域）；
                                                          live 消融（2026-11-08）：body 有 silent token + cookie
                                                          缺失 → verify 读作无 token，仍 403 riskBlock
10 (挑战分支) 预热挑战资产(challengeUrl/stk.ns/captcha.js/df.cfp/fpt.live)
                                                          → solver 得 press token(px3/pxde/pxvid/pxcts)
                                                          → POST risk/verify #2: continuationToken +
                                                          challengeSolution{challengeType:"HumanCaptcha", px3,
                                                          pxde, pxvid} + riskProviderMetadata{riskProvider:"Human"}
                                                          （同一组 press token 双写，pxvid 钉 vid）
11 POST {SIGNUP_API}/CreateAccount                      → body 全字段（SPA JN builder 精确形，2026-11-08 从
                                                          signup-fluent_v2_en chunk 读取 + live A/B 定案）:
                                                          **BirthDate="DD:MM:YYYY"**（$N=day.padStart(2)+":"+month.
                                                          padStart(2)+":"+year；ISO → 1039 field=birthdate 格式拒）,
                                                          CheckAvailStateMap=["memberName:true"]（SPA 同形字符串数组）,
                                                          Country, FirstName/LastName, IsRDM=false,
                                                          IsOptOutEmailDefault/IsOptOutEmailShown(布尔)/IsOptOutEmail,
                                                          IsUserConsentedToChinaPIPL=false（美区；CN consent 标志）,
                                                          LW=false（布尔，非 1）, MemberName, Password,
                                                          **RequestTimeStamp**（大写 S + ISO 字符串，非
                                                          RequestTimestamp/epoch——1350 形状拒的组成之一）,
                                                          ReturnUrl, SignupReturnUrl, SuggestedAccountType="EASI",
                                                          SiteId, **VerificationCodeSlt**（非 VerificationCodeSalt）,
                                                          WReply, uiflvr/scid/uaid/hpgid（wB 层）,
                                                          memberNameChangeCount（驼峰）,（可选 ContinuationToken——
                                                          缺失 → 1349）。
                                                          SPA 实际是三层合并：JN（视图层）+ rB()（风控层
                                                          dfpRequestId/encAttemptToken/RiskAssessmentDetails，来自
                                                          挑战 repMapMetadata）+ wB()（会话层）。
                                                          错误码 live 定案（2026-11-08）：1039=birthdate 格式拒；
                                                          1350=body 形状拒（字段名/类型错；1350/1349 均不在 SPA
                                                          错误 switch 里=服务端形状码）；**1312 field=phoneNumber=
                                                          风险引擎手机验证闸门**（US 出口/dfp 握手重放/PX cookie
                                                          注入均不消；真浏览器 SPA 同出口直接 7005106 硬拒；
                                                          SPA switch 无 1312 → 真流程从不预期。非 body 缺陷）
                                                          （504 视为成功类，不重试）
12 POST {redirectUrl} {slt}                             → 跟随 fmHF 自动提交链(consent ucaction=Yes / KMSI / credentialaction)
13 GET  oauth20_authorize.srf                           → response_type=code + THUNDERBIRD_CLIENT_ID + MAIL_SCOPE
                                                          + login_hint + tenant=common → 提取 code=
                                                          （先 login.live.com，失败退 login.microsoftonline.com/common/oauth2/v2.0/authorize）
14 POST {MS_TOKEN_URL}                                  → grant_type=authorization_code + client_id + code +
                                                          code_verifier(PKCE) → refresh_token/access_token
15 (可选 token#2) 13–14 换 LOGIN_SCOPE 再来一遍          → login_refresh_token（dual 模式）
16 proofs 绑定: 拦截页 frmAddProof                       → POST: iProofOptions=Email, EmailAddress={recovery},
                                                          canary, action=AddProof → 从恢复邮箱读安全码
                                                          （4–8 位数字正则，过滤年份，发件人白名单
                                                          accountprotection.microsoft.com）
                                                          → frmVerifyProof: iProofOptions="OTP||<{email}>||Email||0||t",
                                                          iOttText={code}, action=VerifyProof → 新 SLT POST 收尾
17 passkey 拦截页 → POST postBackUrl                     → error_code="NotAllowedError" 等空字段 → 期望 302
                                                          PasskeyEnrollResult=user_cancel（fallback errcode=1086）
18 读信自检 verify_mail_readable                          → refresh 换 AT + 读 messages 200 → 才算存活
```

## 2. PX 求解事实（一期走打码平台兜底）

- press token 四件套：`px3 / pxde / pxvid / pxcts`；成功判据 `_px3` 含 `:1000:`。
- **silent 变体已验证可用**（slidex，2026-11-08 smoke）：任务不带 challengeUrl/
  uuid/vid，只给 `websiteURL: https://signup.live.com/`（station 由 websiteURL
  单独推断 mode=silent），产出 px3（651 字符、含 `:1000:`）+ pxde/pxvid——
  用于 verify#1 silent 元数据与 `_px*` cookie 播种。appId 任何变体都必须带
  （slidex 强制；真 captcha.run 也接受）。
- 打码平台协议（captcha.run，`POST {base}/v2/tasks/` Bearer key）：
  `captchaType:"PxCaptcha2", uaid, uuid, vid, userAgent, country, timezone`
  + proxy(host/port/login/password) + websiteURL/challengeUrl/appId/cookies；
  轮询 `GET /v2/tasks/{taskId}?captchaType={silent|press}`（press 3s×≤180s、
  silent 2s×≤90s），终态 success/fail/failed/error，取 `response.pressToken`。
- **同 IP 铁律**：solver 出口必须与注册出口同一 sticky 代理（verify 出口 ≠
  collector 出口 → 风控 block，AADSTS riskBlock）。预检要求 3 次采样同出口 IP。
- 纯本地求解（px_wasm/JS 域仿真/SwiftShader）为 stretch goal，不阻塞主体。

## 3. 令牌模式 → 池格式映射（与卡商四段同构）

| 模式 | 产出 | 消费通路 |
|---|---|---|
| `graph` | 四段 `email----password----client_id----refresh_token`（client_id=THUNDERBIRD） | one-mail `graph_outlook`（Graph REST 读信） |
| `login_exe` | 同四段（scope=Thunderbird 全集，可 IMAP/POP/SMTP） | one-mail `imap_outlook` |
| `dual` | 四段 + `login_client_id----login_refresh_token` 六段 | Graph 收码 + SSO |

- refresh 交换统一 `POST {MS_TOKEN_URL}` `grant_type=refresh_token, client_id,
  refresh_token, redirect_uri, scope(可选)`。
- **RT 唯一兑换铁律**（呼应 2026-09-11 one-mail 烧卡事故）：RT 在 AT 有效期内
  复用，禁止并发重复兑换。
- Graph-scoped token 打 Outlook REST 返回 401 InvalidAuthenticationToken——
  两种 scope 不可混用。
- 判活：refresh 得 AT + 读 messages 200 双条件；`invalid_grant` 原文透传判死。
- **Junk folder 显式扫描**（nace 教训）：读信必须 inbox + junkemail 双文件夹。

## 4. 风控与纪律要点

- `{sid}` 粘性会话模板：代理 URL 中 `{sid}` 展开为随机数字串，保证同一账号
  全流程同一出口 IP（signup / risk verify / PX collector / 读信四环节同 IP）。
- country/mkt/lc/timezone/UA 必须与出口 IP 地理一致（chaotic mismatch = 风控信号）。
- 账号间 jitter 分钟级（外部仓默认 3–8 **分钟**，README 口径有秒级样本但
  ANTIBAN.md 明写分钟）；并发 1–2。
- 错误处置事实：risk verify 403 → RiskBlocked（换代理重试）；
  `state=riskInitializationRequired` → 重跑 initialize；
  CreateAccount 504 → 成功类；AADSTS7000110606(riskBlock) → 换代理；
  CreateAccount 1312 field=phoneNumber → 风险引擎手机验证闸门（2026-11-08
  定性，非 body 缺陷——SPA switch 无此码、真浏览器同出口被 7005106 硬拒；
  处置 = 接手机验证资源。**换出口已证无效**：同日 WARP 实验（AS13335
  Cloudflare WARP，proxy=true/hosting=false，3 个不同 IPv6 出口）全 1312，
  加上此前数据中心（HK/IN/FR/US）/住宅（上海移动）全类遍历——闸门对
  出口信誉类不变，不要重试任何出口）。

### 4.1 z_style 会话完备化吸收与消融终局（2026-11-08）

对上游仓做了 14 点源码级 diff 后吸收 P0+P1（全部带单测，981 绿）：

| 吸收项 | 事实 | 实现 |
|---|---|---|
| EvaluateExperimentAssignments | clientExperiments 数组 + canary/telemetry 回刷新，best-effort | `msa_flow.evaluate_experiment_assignments` |
| 风险头 | correlationId/client-request-id=uaid、hpgid/hpgact、Origin/Referer=signup 页 | `_signup_headers` |
| signup query 透传 | CheckAvailable/CreateAccount URL 拼上 signup 页 query | `_signup_api_url` |
| bootstrap 终态 URL | 记 signup_page_url/query | `bootstrap_account_session` |
| msaCreateSignature | z_style verify#1 用 create 签名（无 action，siteId=sSiteId\|\|"292666"，appId=""） | `build_create_signature` |
| silent 元数据 | 无 pxcts 的 4 键数组 | `risk_verify_silent(px_metadata=…)` |
| PX cookie 播种 | token 双通道：body + `_px*` cookie 三域 | `px_client.apply_px_cookies` |
| silent 打码入口 | websiteURL-only 任务 → slidex silent | `px_client.solve_silent_token` |
| 卖家式身份 | 10–12 位小写前缀、11–14 位小写+数字密码、~85 名/~88 姓池 | `_random_prefix/_random_password/_random_name` |

**消融终局（WARP 10881 出口，3 探针递进）**：import 修复 → body 带 silent
token → body+cookie 全套，verify#1 三次全部 `riskBlock_403`；CreateAccount
三次全部 `1312 field=phoneNumber`。即 **z_style 全套会话完备性在本仓复刻后
仍未解除 1312**。

**家宽补测（2026-11-09，用户指令）**：vault 家宽节点 `home-win-CF`（上海移动
住宅，egress-18016 → CF Tunnel → 183.193.161.16，3 次采样稳定）+ z_style
全完备链 + CN 地理一致（country=CN / mkt=2052 / Asia/Shanghai / PIPL=True，
为此补齐 CN timezone 与 `IsUserConsentedToChinaPIPL` 联动 state.country）。
结果：verify#1 仍 `riskBlock_403`（挑战分支 press token 照常通过 verify#2），
CreateAccount 仍 `1312 field=phoneNumber`。**CN 住宅也不解除 1312。**

**越南住宅补测（2026-11-09，同日）**：vault 三处"美国家宽"来源全部失效或
名不副实（BK-美国原生实测 = Akamai Connected Cloud hosting:true；利斯堡
AK 系全在 disabled-nodes；Frontier 16062 HTTP 无响应）——唯一活的住宅出口
是 katabump 续期实战在用的 `[tomorin] [anytls]VN Hanoi VNPT`
（tothost-han-01.telecom.moe:60001，sing-box 单客户端 → gz-cvm 19901，
出口 103.15.90.137，VNPT 住宅 hosting:false，3 次采样稳定，signup.live.com
可达）。z_style 全完备链 + VN 地理一致（country=VN / Asia/Ho_Chi_Minh）：
verify#1 仍 `riskBlock_403`，CreateAccount 仍 `1312 field=phoneNumber`。
**真住宅（hosting:false，实战过 CF Turnstile 的出口）也不解除 1312。**

至此出口矩阵终局：数据中心（HK/IN/FR/US）/ WARP（AS13335）/ CN 家宽
（上海移动）/ VN 真住宅（VNPT，katabump 实战级信誉）全 1312；打码 token
同源排除（上游 captcha.run 系不翻车）。**出口信誉假设被完整证伪**——1312
跟随的不是出口 IP 类，而是流量形态本身（可能：curl_cffi TLS/HTTP2 指纹、
打码 token 与本会话 PX 指纹链断裂、或 MSA 对该自动化形态的整体定性）。
剩余可试变量只剩浏览器级 SPA 自动化（真浏览器 + 真指纹跑完整注册流），
即回到浏览器接管线；或接手机验证打码源。

**新美国家宽终章（2026-11-09，用户供新订阅）**：用户新给美国家宽订阅
（nodes.pigeonw.com，20 节点 type:http :62000）——19/20 活，按 ip-api 分两
类：AT&T Enterprises ×11（23.132.180.x，hosting:false）与 Comcast ×9
（23.230.200.x，hosting:true）。两条新事实：

1. **slidex `proxy` 字段实战证实**（`px/models.py:205,318,336-341` +
   `px/solver.py:1423`：`options["proxy"] = request.playwright_proxy()`）：
   带同代理的 silent 任务确实经该代理出 token（Comcast 003 → px3 587 含
   `:1000:`），同 IP 铁律环境首次可达成。
2. **Comcast 节点 challenge 加载稳定性海选**（3 节/批，直连 signup.live.com
   silent 任务）：019/020 最稳（首测即成，px3=651），009 成，003/006 各
   1成1败（flaky），008/010/011/014 `challenge_load_timeout` 全灭——
   经部分节点 Chromium 加载 challenge 页超时（curl/curl_cffi 同代理却可达）。
3. **AT&T 节点 Chromium 不可用**：001 经代理 signup.live.com 在 slidex
   Playwright 里 45s 超时，同代理 curl 2-4s 302 / curl_cffi chrome 伪装 200。

**同 IP 铁律决定性测量（Comcast 019，<creds>@23.230.200.100:62000）**：
z_style 全完备链，silent token 由 slidex 经**同一** Comcast 019 代理铸出
（`silent_px: ok=True`），press 分支 verify#2 照常出 token
（`risk_verify_press: has_token=True`），但 verify#1 仍 `riskBlock_403`、
CreateAccount 仍 `1312 field=phoneNumber`。**同 IP 铁律满足后 1312 依旧
——token 出口错配假设亦被证伪。**最终结论：1312 归因收敛到流量形态本身
（curl_cffi TLS/HTTP2 指纹 / solver token 与会话 PX 指纹链断裂 / MSA 对
该自动化形态整体定性），浏览器级 SPA 自动化或手机验证打码源是仅存路径。

### 4.2 浏览器级 SPA 自动化消融终局（2026-11-10，否定终局）

用户批准走浏览器路线后，同日完成 5 层消融，每层全部 `riskBlock`：

1. **SPA 流程摸底**（Playwright Chromium-1248 headless，gz-cvm，
   Comcast 019）：真浏览器流程 = email → password → **Add some details**
   （国家 + 生日，自定义 combobox `#countryDropdownId/#BirthMonthDropdown/
   #BirthDayDropdown` + `input[name=BirthYear]`）→ **Add your name**
   （`#firstNameInput/#lastNameInput`）→ risk/verify → CreateAccount。
   `CheckAvailableSigninNames` 全程 `isAvailable:true`，email/密码/生日/
   姓名四页全过。
2. **消融①headless vs headed-Xvfb（019 脏 IP）**：均
   `verify 403 riskBlock` → "Account creation has been blocked" 页。
3. **消融②新节点 020**（仅 1 次 slidex mint、零注册污染）：仍 riskBlock
   ——IP 量级混淆排除。**verify 200 是 OPTIONS CORS 预检**，POST 直掫
   `AADSTS7005106 riskBlock`，无任何挑战入口。
4. **消融③真 Chrome 157**（gz-cvm /opt/chrome-stable 手动解 deb；磁盘满
   100% 清 /tmp 5.6G 后装）+ `--disable-blink-features=AutomationControlled`
   + 抹 `navigator.webdriver`：仍 riskBlock。
5. **消融④真 Windows 真 Chrome 真桌面**（home-win，playwright 1.60 +
   `C:\Program Files\Google\Chrome\Application\chrome.exe` headed）+
   Comcast 020：仍 riskBlock。WinUI combobox 需 `force=True` click
   （普通 click actionability 卡 30s）。

**终局结论（叠加 §4.1 五类出口矩阵）**：从纯协议（curl_cffi）到真 Windows
真 Chrome，全谱系自动化形态 100% 被 MSA 风控终局拦截——协议线
`1312 field=phoneNumber`、浏览器线 `AADSTS7005106 riskBlock`（硬拒，无
挑战/手机验证入口，**SMS 打码源无可接入点**）。且上游 lxf746 仓的成功
样本无法在当前时间点复现（其 issue 区无 2026-11 后的成功报告佐证）。
Outlook 自注册产线**至此关闭**：剩余路径仅（a）人工/半自动注册产号，
（b）外购号源灌池（维持既有 mail_credentials 模式），（c）等待 MSA
风控策略变化后重探。探针资产：/tmp/probe_spa3.py（可配置代理全流程）、
/tmp/spa_fullbody.py（verify 全响应体诊断版），gz-cvm + 本地各一份；
真 Chrome 装机 /opt/chrome-stable（gz-cvm，50G 盘已清出 4.7G）。

### 4.3 代码审查翻案与真人对照（2026-11-10，最终判定）

用户质疑「是不是代码有问题」后深查，修正了浏览器线的失败归因：

1. **代理代码链审查干净**：session.py（curl_cffi 全局 proxy，CONNECT
   域名在代理端解析，无 DNS 泄漏）、px_client.py（`_proxy_fields` 拆分
   正确，slidex 实测认 proxy 字段）。
2. **但浏览器探针有 3 个真实缺陷（已修复）**：
   - **WebRTC 泄真 IP**（最严重）：STUN srflx 候选绕过代理——gz-cvm
     探针泄漏腾讯云 134.175.149.246，home-win 泄漏上海电信真 IP。页面
     说「Comcast US」、WebRTC 说「CN 机房」= 代理 VPN 一击即中。修法 =
     Chrome 原生策略 `--webrtc-ip-handling-policy=disable_non_proxied_udp`
     （实测 srflx 清零；比 JS Proxy 覆盖更优——RTCPeerConnection 真实
     无补丁痕迹）。此前「SMS 无接入点」的结论是在泄漏环境下测出的，
     归因需下修。
   - **入口 URL 陈旧畸形**：硬编码 `rpsnv=173&id=293289`；真人路径 =
     account.microsoft.com → AREA_CONFIG `createAccountUrl`（`rpsnv=208`
     + `ct` 时间戳 + `wreply` 回跳链）；修复时还须解码 JSON `\u0026`。
   - **时区错 3 小时**：Comcast 节点全部 Santa Clara（LA），探针却设
     New_York。
3. **全修复 v4（真人路径 + LA 时区 + 拟人节奏/鼠标/逐字输入）仍
   riskBlock**（014/010）。AT&T 004 再证 Chromium 页面水合失败不可用。
4. **用户真人对照实验（决定性）**：手动 Chrome + 014 代理 → **拿到
   press-and-hold 挑战**（非硬拒）→ 按住/进度条完成/网络请求后**反复
   判不过，无限循环**。即这批 23.230.200.x/23.132.180.x「家宽」订阅段
   实为 ISP-proxy 机房段，MSA 双层处置：**真人给不可赢的挑战循环、
   自动化直接硬拒**。
5. **最终判定**：浏览器线失败 = IP 段深标记（主因，真人也过不去）+
   Playwright CDP 可检测性（次因）。协议线唯一残墙 = **1312 phone**
   ——而 verify#2 press token 在 Comcast 019 已实证可过（has_token=True），
   即协议线挑战环节是通的。**产品线重启钥匙：SMS 接码源接入 1312 手机
   验证步 + 换未标记真住宅出口（US 4G 移动 / 真家宽）**。此前「SMS 无
   接入点」仅对浏览器硬拒场景成立，协议线 1312 场景 SPA 本就提供手机
   验证 UI。探针资产新增：/tmp/probe_spa4.py、/tmp/spa_leaktest.py。

### 4.4 电话门（1312）协议事实与产线接入（2026-11-10，方案 A 代码层）

端点与格式全部从 signup SPA 公开资产提取（signup-fluent_v2 bundle +
live 页面 ServerData，2026-11-10；只取端点/字段/流程事实）：

1. **端点**：`POST {SIGNUP_API_BASE}/Proofs/SendOtt`（发 SMS 码）、
   `POST {SIGNUP_API_BASE}/ConsumeOneTimeToken`（验码）。URL 追加 bootstrap
   页 query（与 CheckAvailable/CreateAccount 同规）。
2. **body（JSON）**：共享 GB 构造器 = `{...params, destinationPii,
   uiflvr, scid, uaid, hpgid}`；`destinationPii = "+" + country + national`
   （国内号前导 0 剥除，`^000\d+` 保留）。Send 侧 ServerData：
   `sSendOttAction="SignUp"`、`sSendOttPurpose=""`；Consume 侧（bundle
   原文）：`{action:"OnlyVerifyNoConsume", channelType:"SMS", ott:<code>,
   ottPurpose:"VerificationCode"}`——OnlyVerifyNoConsume 保持 proof 未消费，
   由后续 CreateAccount 重试在服务端消费。
3. **头**：`Content-Type: application/json; charset=utf-8` +
   `Accept: application/json` + `canary: <apiCanary>` +
   `correlationId/client-request-id: <uaid>` + `hpgid/hpgact`（与
   `_signup_headers` 现有集合一致）。
4. **ServerData 值**：`hpgid=200225`、`iScenarioId=100118`（scid）、
   `iUiFlavor=1001`（uiflvr）、`sPrefSMSCountry="US"`。

产线实现（方案 A，全部离线测试覆盖）：

- `providers/outlook/protocol/sms_client.py` — `SmsProvider` 接口
  （acquire → wait → settle）+ sms-activate `handler_api.php` 兼容适配
  （base/service 可配，`OUTLOOK_SMS_API_KEY` 等仅经 env，key 永不落日志）；
  `create_sms_provider()` 无 key 返回 None（无源时 1312 行为不变）。
  号码纪律：只在 WAIT_TIMEOUT_S=240s 内持有；成功 complete(status=6)、
  失败/超时一律 cancel(status=1)（无短信进账平台退款）。
- `msa_flow.py` — `create_account` 识别 `error.code=="1312"`（dict）→
  `kind="phone_gate"`（字符串 error 不受影响）；新增 `send_ott` /
  `consume_ott` / `phone_verify_and_recreate`（acquire→send→wait→
  consume→CreateAccount 重试，全出口 settle，号码只落 masked 形态）；
  `register_one(sms_provider=…)` 电话门自动重试接线。
- `outlook_adapter` — `options.sms_provider: env`（默认）→ env 解析，
  缺省空 → 关闭。
- 测试 +12：1312 判定×2、SendOtt/Consume body+URL+头×3、
  phone_verify_and_recreate 成功/释放×2、sms-activate 适配×5。
  全套 994 passed。gz-cvm 已同步。**live 联调待用户提供接码平台 key。**

### 4.5 live 联调与暂缓决定（2026-11-10，用户拍板暂缓）

live n=1..4（gz-cvm，manual SMS provider 读 /tmp/sms_code.txt 等真人码）：

1. n=1（US 流 +86 号，destinationPii body）：`6002=InvalidArguments`
   —— 发送 body 形态错了（后来 bundle 取证确认 SendOtt 走 AB 直发）。
2. n=2（CN 流 PIPL + CN 家宽 183.193.161.16，同 body）：同 6002。
3. **body 形态修正**（bundle 原文）：SendOtt = AB 直发
   `{action:"SignUp", channel:"SMS", proofId:<国内号>, proofCountryIso:<iso>}`
   （与 Consume 的 GB `destinationPii` 形态**不同**——两套 body 是本案
   最大的坑）。测试同步改型，994 保持全绿。
4. n=3/n=4（CN 流，修正后 body，proofId 格式变体矩阵）：
   `<national>|CN` / `<dial><national>|CN` / `+<dial><national>|CN` /
   `+861865866682|`(无 iso→1348 缺参) —— 全部 `1208=SmsNumberFormatInvalid`
   （错误枚举 bundle 原文：1208 格式无效 / 1067 国家被禁 / 1221 发码过多 /
   6002=InvalidArguments）。CN 页 ServerData 与 EN 页同构（SendOtt/
   Consume 同端点、`sPrefSMSCountry:"CN"`、CN bundle 同 AB 形态）——
   **请求形态无懈可击，1208 是服务器对 CN 号的语义拒绝**（可能：CN 号
   不被 signup 验证接受 / 该号已绑定他账号），协议侧无法再分辩，需真人
   手动流的 devtools 真值捕获。

**用户决定：Outlook 注册机暂缓（2026-11-10）。** 终态：

- 代码层完整保留（电话门 + SMS 源接口，994 测试全绿，gz-cvm 同步）；
  重启只需接码平台 key（`OUTLOOK_SMS_API_KEY` 等 env）+ 可能一次真人
  手动流的 SendOtt 真值捕获（若 1208 需要进一步分辩）。
- 个人号码相关临时探针已删除（probe_phone_gate.py /
  probe_phone_variants.py / sms_code.txt）；GPL 参考 clone 已删。
- 保留资产：探针 probe_spa3/spa4/spa_fullbody/spa_leaktest（gz-cvm +
  本地）、真 Chrome 装机 /opt/chrome-stable（gz-cvm）、美国家宽订阅
  节点表（本地 /tmp/us_nodes_creds.json）。
- 池侧补货回到既有模式：外购号源验活灌池（轨道 B 已完成的
  replenish/append/flock 链路）。

## 5. 重写实现索引（本仓）

- `providers/outlook/protocol/constants.py` — §0 常量表
- `providers/outlook/protocol/msa_flow.py` — §1 十八步编排
- `providers/outlook/protocol/px_client.py` — §2 打码平台兜底
- `providers/outlook/protocol/mail_client.py` — §3 令牌/读信（Graph REST + Junk 扫描）
- `providers/outlook/protocol/pacing.py` — §4 步间抖动 + 冷启动
- `register_core/providers/outlook_adapter.py` — `options.mode: protocol|browser`（默认 protocol）
