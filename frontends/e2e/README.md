# 验收 E2E：模拟租户会话的续期隔离（S1 / S2 / S3 / S7 / S9）

契约：[docs/auth/tenant-impersonation-refresh-isolation-plan.md](../../docs/auth/tenant-impersonation-refresh-isolation-plan.md) §4 BDD、§5 ATDD。
归属子单：`jonex-jo13`（父单 `jonex-hlb2`）。

这些是 **ATDD 外循环用例**：依赖 `jonex-r17g`（后端标记 + 拒绝）与 `jonex-3pjk`（前端会话），
两者落地前**红灯是预期的**。⛔ 不要为了让它们变绿去改产品代码；也不要改断言迁就实现。

## 与单测的边界

|          | 位置                                                                      | 归属   |
| -------- | ------------------------------------------------------------------------- | ------ |
| 前端单测 | `frontends/shared/shell-sdk/**/*.test.ts`、`frontends/shell/**/*.test.ts` | 子单 B |
| 验收 E2E | 本目录                                                                    | 子单 C |

## 前置条件（缺一不可）

1. **完整栈在跑**：Gateway(8000) + Sidecar(8001) + platform(8006) + 前端。
   本套用例**不启动任何服务**（配置里没有 `webServer`），请用 VSCode Debug
   `Jonex: Full Stack (无 RAG)` 或 `make dev-frontend*` 自行拉起。
2. **两个租户存在**：`tenant_jonex_demo`（登录租户）与 `tenant_jonex_sso`（目标租户），
   且登录账号是**平台管理员**（`impersonate()` 只认 `isPlatformAdmin`，不看登录方式）。
3. **`IMPERSONATE_EXPIRE_MINUTES` 调小**（模拟 token 默认 30 分钟，等不起）：

   | 变量                              | S1 / S2 / S9 轮 | S3 / S7 轮 |
   | --------------------------------- | --------------- | ---------- |
   | 后端 `IMPERSONATE_EXPIRE_MINUTES` | `2`             | `1`        |

   改 `deploy/.env`（或本机 `.env.local`）后重启 platform 服务；E2E 侧用
   `E2E_IMPERSONATE_EXPIRE_MINUTES` 对齐，两侧不一致时用例会**显式 FAIL**（而不是给假结论）。
   ⚠️ S2 要求 ≥2（否则观察不到「提示前」的基线）；S3/S7 要等真到期，设 1 最快。

4. **前端 build 是最新的**：docker 部署下 `shared/shell-sdk` 的改动不会随后端 rebuild 生效，
   需 `make rebuild-service SERVICE=shell-frontend`（见 memory/browser-e2e-verification-playwright.md）。

## 安装

本目录是 pnpm workspace 的一个包（`@jonex/e2e`）。

```bash
cd frontends
pnpm install                       # 首次：把 @playwright/test 链进来（会更新 pnpm-lock.yaml）
pnpm exec playwright install chromium   # 首次：下载 chromium（~160MB）
```

## 环境变量

| 变量                             | 默认                                | 说明                                                               |
| -------------------------------- | ----------------------------------- | ------------------------------------------------------------------ |
| `E2E_BASE_URL`                   | `http://localhost:5173`             | 前端入口；用 dev-gateway 时设 `http://localhost:8080`              |
| `E2E_TENANT`                     | `tenant_jonex_demo`                 | 登录租户（也是模拟态的原租户）                                     |
| `E2E_USERNAME` / `E2E_PASSWORD`  | 无（必填）                          | 平台管理员账号，本地密码登录                                       |
| `E2E_TARGET_TENANT`              | `tenant_jonex_sso`                  | 模拟目标租户                                                       |
| `E2E_IMPERSONATE_EXPIRE_MINUTES` | `2`                                 | 必须与后端一致                                                     |
| `E2E_S1_OBSERVE_MS`              | `15000`                             | S1 的「零 refresh」观测窗口                                        |
| `E2E_401_PROBE_PATTERN`          | `**/api/v1/auth/me`                 | S9 用来制造 401 的业务接口                                         |
| `E2E_AUDIT_ACTION`               | `impersonate_expired`               | S3 要核验的审计动作                                                |
| `E2E_TOAST_TEXT_PATTERN`         | `模拟\|impersonat`                  | 到期提示文案匹配（中英兼容）                                       |
| `E2E_TOAST_SELECTOR`             | `.ant-message-notice`               | 版本无关容器类名（v5/v6 共有）                                     |

```bash
cd frontends/e2e
E2E_USERNAME=admin E2E_PASSWORD='<密码>' E2E_IMPERSONATE_EXPIRE_MINUTES=2 \
  pnpm run test:fast          # S1 + S9（不等到期，约 1 分钟）
```

## 两轮跑法

```bash
# 第一轮：S1 / S2 / S9（后端 IMPERSONATE_EXPIRE_MINUTES=2）
cd frontends/e2e
E2E_USERNAME=admin E2E_PASSWORD='***' E2E_IMPERSONATE_EXPIRE_MINUTES=2 pnpm exec playwright test \
  specs/s1-impersonation-no-refresh.spec.ts specs/s9-impersonation-401.spec.ts specs/s2-expiry-warning.spec.ts

# 第二轮：S3 / S7（后端改成 1 并重启 platform 后）
E2E_USERNAME=admin E2E_PASSWORD='***' E2E_IMPERSONATE_EXPIRE_MINUTES=1 pnpm exec playwright test \
  specs/s3-auto-exit.spec.ts specs/s7-post-exit-refresh.spec.ts
```

预计时长：S1 ≈ 25s + 登录；S9 ≈ 20s；S2 ≈ 2min；S3 ≈ 1.5min；S7 ≈ 1.5min。

## 被测元素契约（`data-testid`）

⛔ 用 testid 而不是文案：**antd 是 v6**，两字按钮名会被插入空格，文案定位会假 FAIL。
定位**只剩 testid 一条路 —— 回退链已退役**（曾经回退到 `role=button` + 文案、`data-menu-id`、
`role=alert`，那些都是会被 antd 升级打破的隐式契约）。缺失时**显式 FAIL**（`MISSING_TESTID_HINT`），
不静默跳过。

| testid                                   | 用途                                   | 落点（产品代码）                                                                |
| ---------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------- |
| `impersonation-countdown`                | 横幅内的倒计时节点，只在最后 60 秒出现 | `shell/src/components/AppShellLayout/index.tsx`（横幅 `Alert` 的 message 内）   |
| `impersonation-banner`                   | 模拟态顶部常驻横幅                     | 同上，挂在该 `Alert` 上（透传到带 `role="alert"` 的根 `div`）                    |
| `tenant-impersonation-switcher`          | 顶栏「切换租户」触发器                 | `shell/src/components/TenantImpersonationSwitcher/index.tsx`（antd `Button`）   |
| `tenant-impersonation-option-<tenantId>` | 下拉中的目标租户选项                   | 同上，挂在 `Dropdown` 的 `menu.items[].'data-testid'`（透传到 `<li>`）           |

> testid 值是**逐字符契约**：产品代码里的字面量与 `lib/harness.ts` 的 `TESTID` 必须一致。
> 改动任一侧都要同步另一侧，改完至少跑一遍本目录用例证明还定位得到。

## 各 Scenario 的断言落点

| Scenario                     | 断言                                                                                                                                                                                                                                                                                                  |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **S1 模拟态不续期**          | `page.on('request')` 全程记录 ⇒ `/api/v1/auth/refresh` **命中数组必须为空**（判据是「未发出请求」，不是最终 token 值）。另断言 `jonex_access_token` 未变、payload `tenant_id=目标租户` 且 `impersonated=true`、`jonex_impersonation_backup` 仍在且属原租户、不跳 `/login`、不发 `jonex:token-expired` |
| **S2 到期前 60s 提示一次**   | 基线（距到期 >60s）`impersonation-countdown` 不存在且无提示 ⇒ 跨过 exp-60s 后倒计时**出现**、匹配文案的 Toast 数 `===1` ⇒ 到 exp-6s 仍是 `1`（按**出现次数**计，不是按文案去重）                                                                                                                      |
| **S3 到期自动退出**          | 到期后 `accessToken` 的 `tenant_id=原租户` 且 `impersonated` 非真、不跳 `/login`、`audit-logs` 里能找到 `impersonate_expired`、退出后**真实业务请求**的 Bearer 属原租户且不带模拟标记                                                                                                                 |
| **S7 退出后原会话可续期**    | 退出后让应用自己发起一次续期 ⇒ 捕获到的 refresh 响应 **status=200**、响应体含 `access_token`、不跳 `/login`、`jonex_access_token` 被更新                                                                                                                                                              |
| **S9 模拟态 401 不触发登出** | 把 `/auth/me` 拦成 401（`switchTenant` 内部必经的 axios 调用）⇒ 不发 `jonex:token-expired`、`jonex_impersonation_backup` 仍在、不跳 `/login`                                                                                                                                                          |

### 反假绿措施（本仓踩过多次的坑）

- **S1 的正对照**：主断言（零 refresh）之后，在页面里手工 `fetch('/api/v1/auth/refresh')`，
  断言采集器计数从 0 变 1 ⇒ 证明「采集器 + URL 判据」有效，否则那条「零 refresh」无判别力。
  另断言切换后启动期间 `net.all.length > 0`（采集器全程是活的）。
- **S9 的正对照**：判「没发事件」之前，先手工派发同名 `jonex:token-expired` 并断言采集器听得到；
  再断言应用自己的「事件 → 跳登录页」链路确实会跳 ⇒ 两次缺席断言才有判别力。
- **S2 的正对照**：倒计时用「先断言不存在、后断言存在」成对断言；Toast 检测器先跑自检
  （注入一个 `.ant-message-notice`，确认检测器测得到「出现」，然后清干净）。
- **反空洞**：S9 断言 401 **真的发生过**（探针命中数 > 0）；S3/S7 断言退出后/续期请求**真的发生过**。
- **不用 `page.evaluate` 观测导航**：跳转链用 `page.on('framenavigated')` 累积
  （导航挂起时 `evaluate` 会被阻塞、读到过期状态 —— 见 memory）。

## 已知覆盖差异 / 与契约不符处（需要人拍板）

1. **「临近过期」是模拟的**：S1 把 `jonex_token_expires_at` 种成过去值来复现契约 §0 的「加速器」，
   而不是等 30 分钟。这是缺陷必现的窗口，但没有覆盖「到期时间戳正常、仅靠模拟态判据短路」这一半
   —— 那一半由 S1 观察窗口（≥15s，期间 token 未过期且严禁任何 refresh）覆盖。
2. **S2 的提示文案**按正则匹配（`E2E_TOAST_TEXT_PATTERN`，默认含「模拟」），
   若实现用「会话即将结束」等完全不含「模拟」的文案，需要调这个变量。
3. **S3 的审计核验**走 `GET /api/v1/platform/audit-logs`（先带 `action` 过滤、再退回无过滤翻首页）。
   审计最终落在哪个租户的 scope 里契约没写死；若查不到，用例会打印端点 + 状态 + 响应摘要，
   便于判断是「真没写」还是「查错租户/接口口径不同」。
4. **S7 的续期触发是「种过期时间戳 + reload」**，而不是等一次真实业务 401（契约 §3.5 说正常触发是下一次 401）。
   两者走的是同一个 `callRefresh()` 通道，但 §3.5 那条路径本身没有用例覆盖。
5. **元素定位只认 testid**（见上表）：四个 testid 都已落在产品代码里，回退链已退役。
   若 testid 被删/改名，用例会显式红灯并提示 `MISSING_TESTID_HINT` ——
   正解是补回同名 testid，不是加回文案/`data-menu-id` 那种更脆的定位。
6. **未覆盖**：S4/S8/S5/S6 属后端 pytest 集成（子单 A / D），不在本目录。
