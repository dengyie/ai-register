# z.ai provider（ZCode Coding Plan 注册）

chat.z.ai 两段式邮箱注册：signup（滑块×2）→ 验证链接 → Complete Registration →
真实 JWT。产物是 `secret_kind="jwt"` 的 chat.z.ai 会话 JWT——hub（zcode2api）
入池属**操作员显式命令**，绝不自动写生产池。

## 战斗语义（2026-11-16 双号实弹定版，改代码前先读）

| 铁律 | 内容 |
|---|---|
| 成功收据链 | signup POST 200（本会话 NET tap）→ 验证链接到信（unified admin API 按 `to_addr` 客户端过滤 + baseline 去重 + 全参数门禁）→ JWT email claim == 本号邮箱。三重门禁 immune to slidex 假成功（F001 `success:true,verifyResult:false` 时 solve 仍返 True） |
| 会话轮换 | 滑块不稳定是常态；卡死/失败直接换新会话（cookie_id 轮换），勿在原会话反复挣扎。`gap=0px travel=0px` + `piece alpha 全不透明` 假检测家族 = 换会话即过 |
| 网名 | **纯 `[a-z0-9]`**（one-mail `new_address` 写入侧剥非字母数字字符，带点/下划线会致「JWT 名与收信 to_addr 错位」→ 信不可见）+ 自然感词根（反 `zcoderegN` 顺序化）+ 双查重（one-mail 地址表 + hub 池 label） |
| 邮箱 | catch-all 前缀发明（**无需 new_address 行**，one-mail catch-all 任意前缀直达入库）；读取走 unified admin API 客户端过滤，与地址行/JWT 解耦 |
| 探针红线 | 勿用 `hi`/`hello` 短探针测活（上游反测活拦截）；本 provider 不做 messages 探测 |
| token 过期 | 验证链接 token 实测 1h+ 已过期（上游未披露上限）——拿到链接立即补完；过期表现 = verify_email POST 400 + 页面 "token is invalid or has expired" → 重触发 signup 重发再取新链接 |

## 运行

预算语义：CLI/`ZAI_BUDGET` 的 `timeout_s` 是**单次注册整体预算**——flow 在
attempt 间检查剩余，单会话超时取 `min(480s, 剩余)`，剩余不足 90s 即终止重试。

```bash
# 注册（一条命令，全自动：预检→开号→注册→收据→补完→归档 jsonl；hub 不自动动）
./register.sh zai [count]
# 或直接
./providers/zai/run-register.sh 1

# 操作员显式入池（OAuth 四步直投 + 池核验门禁）
./register.sh zai-join <token-file> [label]
./register.sh zai-join --verify-only <label>     # 只跑池核验
```

## 环境

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZAI_PYTHON` | 自动 | 解释器须带 slidex（含 `aliyun_captcha_js`，≥0.11）+ playwright + **有头 GUI**；解析顺序：ZAI_PYTHON → repo .venv（能 import aliyun_captcha_js 才用）→ `~/project/slidex/.venv` |
| `ZAI_DOMAIN` | `dengyie.tech` | one-mail catch-all 域 |
| `ZAI_HANDLE` | 自动生成 | 固定网名（缺省自动生成唯一网名） |
| `ZAI_ATTEMPTS` | 3 | 换新会话次数 |
| `ZAI_LINK_WAIT` | 150 | 单轮等验证链接秒数 |
| `ZAI_SESSION_TIMEOUT` | 480 | 单会话硬超时（卡死强撤，finally 关浏览器） |
| `ONEMAIL_ADMIN_KEY` | 必填 | one-mail unified admin API（`x-admin-auth`） |
| `ZAI_MAIL_API` | `https://mail-api.mangoqwq.cc.cd` | one-mail origin |
| `ZAI_HUB_URL` / `ZAI_HUB_KEY` | — | hub（**必填**：网名唯一性预检 + zai_hub verifier 都依赖）；缺 key 时 run-register.sh 直接 fail-fast |
| `ZAI_BUDGET` | — | 单次注册整体预算秒数（覆盖 CLI `--timeout` 语义；缺省 = attempts×480s 自然上界） |
| `ZAI_POOL_VERIFY_WAIT` | 180 | 池核验轮询上限（首轮 billing 前 quota 为空属预期） |

## 产物

- sink jsonl（`providers/zai/output/pipeline.jsonl`，0600）：`to_sink_dict` 全量（含 JWT）
- stdout 只见 `to_public_dict`（密码/JWT 永不完整出现）
- artifacts 会话标记：`cookie_id / signup_posted_at / verify_link_sha8 / token_email / attempts_used`——`flow.validate_this_run` 拒收无标记的伪造成功

## 已知边界

- 有头浏览器：需 macOS GUI 会话（或 linux xvfb）。无 DISPLAY 的 linux 直接 fail-fast
- 滑块求解依赖 slidex 修 refactor：F001 假成功契约修复见
  `~/project/slidex/docs/slider-humanization-requirements-2026-11-16.zh-CN.md`（R7 修复后
  adapter 无需改动——放行判定本来就在本层，不信 solve 返回值）
- `register_core/verify/zai_hub.py` 是**首日核验**语义（双模型额度满）；存量号额度被
  真实流量耗尽后永不满足，勿用于存量巡检
- 救号路径（注册完成但验证信错位不可见的历史号）：re-signup 重发 → unified admin API
  客户端过滤取新链接 → `run_complete_session`；实战记录见 vault z-ai 注册最佳实践 § 2026-11-16
