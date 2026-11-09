# ANTIBAN — Outlook 注册防封纪律（protocol mode 专项）

适用面：`options.mode: protocol`（providers/outlook/protocol/）。原则与通用
注册机纪律一致（Grok/ChatGPT 路径同样适用），但数值与信号是 Outlook/MSA
风控专项的。协议事实依据见 `docs/outlook-protocol-notes.md` §4。

## 1. 同 IP 铁律（最强信号）

MSA 风控链四个环节——signup 页面加载、risk/verify 提交、PX collector 上报、
mail OAuth + 读信——**必须走同一个 sticky 出口 IP**。verify 在 A 出口校验、
collector 在 B 出口提交是最经典的 AADSTS riskBlock（AADSTS7000110606）来源。

操作纪律：
- 代理用 `{sid}` 粘性会话模板（rapidproxy 风格），一个账号一个 sid，全程复用；
- 跑批前预检：3 次采样出口 IP 必须一致，不一致拒绝在非粘性代理上轮换
  （除非显式 `OUTLOOK_ALLOW_ROTATING_PROXY=1`，不推荐）；
- 打码平台请求也带同一个 proxy 字段——collector 出口 ≠ 注册出口同样判 block。
- strategy.burn `[ip, proxy]`：risk_blocked / proxy 连续失败 → 整对出口烧掉
  冷却。pipeline 把代理写 `extra["proxy"]`、把出口 IP 写 `extra["_egress_ip"]`
  （TTL 缓存探测，register_core/util/proxy.py），StrategyEngine 自动接线。

## 2. 地理一致性五件套

出口 IP 国家、`country`（决定 mkt/lc）、IANA timezone、UA（Chrome/Windows）、
打码 payload 里的 country/timezone——**五者必须一致**。DE 出口配 EN-US/mkt
是美国住宅 IP 上跑欧洲号码，属于便宜的风控信号。constants.py 的
`mkt_lc_for_country`/`timezone_for_country` 从同一个 country 派生，勿手动拆开配。

## 3. 节奏（分钟级账号间隔，不是秒级）

- 账号间隔：默认 **3–8 分钟**（`OUTLOOK_REG_JITTER_MIN/MAX`，单位分钟）。
  外部仓 README 有秒级样本口径，但其 ANTIBAN.md 明写分钟——按分钟执行。
- 并发：默认 **1–2**，最多 2–3。同出口 IP 并发注册是批量最强信号。
- 步间 jitter：3–8 秒（`OUTLOOK_STEP_JITTER_MIN/MAX`），pacing.human_pause
  已内置在 bootstrap / check_available / risk / create_account 之后。
- 冷启动：批次第一个账号前 30–90 秒（`OUTLOOK_COLD_START_MIN/MAX`）。

## 4. 刷新克制（RT 唯一兑换）

呼应 2026-09-11 one-mail 烧卡事故：**一个 refresh token 同一时刻只允许一个
在途兑换请求**；AT 未过期就复用 AT，不要反复用 RT 换新 AT。MSA 的 RT 轮换
语义（新 RT 签发后旧 RT 短期存活）+ 并发兑换 = 触发吊销，号死在池里。

- mail_pool_probe 的写回原语已按 flock 串行化；不要绕过它手写并发刷新。
- `invalid_grant` 是终态：原文透传、可隔离（quarantinable）、**永不重试**。
- Graph scope 与 Outlook REST scope 互不通用（401 InvalidAuthenticationToken）；
  mail_token_mode 决定读信通路，不要拿 graph token 打 outlook.office.com。

## 5. 新号冷启动与读信

- 注册完成后先读信自检（refresh → AT → messages 200 双条件）再入池；
  死号/只读不通的号不灌 mail_credentials.txt（scripts/replenish_pool.py
  的 caller-guard 已实现该语义）。
- 读信**必须双文件夹**（inbox + junkemail）：OTP 邮件常落 Junk（nace 教训），
  mail_client.read_otp_code 默认扫描两处，勿只读 inbox。
- 新号 24h 内是风控观察期：观察期内不要高频用 Graph 全量拉信（每次
  refresh/读信都是信号），消费侧按需拉取。

## 6. proofs 与恢复邮箱

- **禁止 skip proofs**：不绑恢复邮箱的号封号率显著更高（外部仓实测口径）。
- **当前状态（2026-11-07）**：protocol 模式**尚未实现** proof 绑定
  （`bind_recovery_email` 是 browser 路径的 patchright 页面流，协议路径没有
  对应 HTTP 实现）。这是已知限制而非已生效的纪律——放量前必须实现，或
  用 live 存活率数据决定 protocol 模式的产量上限。跟踪：vault §12.4。
- 恢复邮箱一号一箱（vault 红线：禁止 plus-alias 邮箱农场）。

## 7. 错误处置速查（协议事实）

| 信号 | 处置 |
|---|---|
| risk verify HTTP 403 | RiskBlocked → 换代理重试（burn 该出口对） |
| state=riskChallengeRequired | 走 press 挑战（px_client） |
| state=riskInitializationRequired | 重跑 initialize（自动，一次） |
| CreateAccount 504 | 成功类，不重试 |
| AADSTS7000110606 (riskBlock) | 出口错位 → 检查 sticky，换代理 |
| invalid_grant | 终态，隔离，不重试 |
| errcode=1086 / error.aspx | passkey 拦截 fallback，非封号信号 |
