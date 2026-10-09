# 模拟单点登录网址生成器

`scripts/local-dev/sso_login_url.py` —— 一条命令拿到一条可用的 SSO 登录网址，不用再找人现生成。

## 解决什么问题

调试 SSO 链路时经常需要一条「打开就能走完整登录」的网址。手工拼不行（原因见下），本脚本向本机 Gateway 的发起端点**现取**一条，输出网址 + state + 有效期。

## 快速开始

```bash
# 零参数，最常用
python3 scripts/local-dev/sso_login_url.py

# 也可以用仓库 venv（与其它本地脚本一致）
.venv/bin/python scripts/local-dev/sso_login_url.py
```

脚本只用 Python 标准库，裸 `python3` 即可运行，不需要额外装依赖。

其它常用形式：

```bash
python3 scripts/local-dev/sso_login_url.py --list                      # 看有哪些可用落点
python3 scripts/local-dev/sso_login_url.py -r http://localhost:8080/   # 指定登录后落点
python3 scripts/local-dev/sso_login_url.py --idp casdoor               # 直接跳外部身份源
python3 scripts/local-dev/sso_login_url.py -g http://172.30.2.57:8000  # 换 Gateway 地址
```

把输出的网址粘进浏览器即可。**拿到后尽快打开**——详见「有效期与一次性语义」。

## 输出怎么读

```
① jonex 发起端点（浏览器直接打开，走全链路）
② Keycloak authorize（后端 302 的 Location）
③ 同上 + kc_idp_hint=...（仅在传了 --idp 时出现）
```

- **①** 是标准入口。浏览器打开后后端生成一次性 state 并 302 到 Keycloak 登录页。
- **②** 是 ① 的响应副产品（后端 302 的 `Location` 头），**同样可以直接打开**，二者等价。它多出来的价值是：排查问题时能直接看到 realm / client_id / PKCE 参数，也方便手工追加 query 参数。
- **③** 只在 `--idp` 时输出，在 ② 基础上追加 `kc_idp_hint`，跳过 Keycloak 登录页直接跳外部身份源（demo 场景是 Casdoor）。**state 不变，仍是后端签发的一次性票据，链路照样通。**

输出里同时打印 state、有效期、租户 → realm 映射、登录后落点及其来源。

## 落点怎么选（最容易白登录一场的地方）

白名单是 `{应用: [落点, ...]}`。**看一个落点被几个应用声明**，就能分出两类：

| 类型 | 判据 | 例子 | 能不能接住 SSO 登录 |
|---|---|---|---|
| **宿主入口** | 被**多个**应用共同声明 | `http://localhost:8080/`、`http://localhost/` | ✅ 能 |
| **调试入口** | 只被**一个**应用声明 | `http://localhost:5175/`（core-business standalone） | ❌ 不能 |

原因是 SSO 的登录态交接方式：回调只下发一枚 **httpOnly refresh cookie**，不发 access token。
**宿主 shell** 启动时会拿这枚 cookie 走 `/auth/refresh` 换 access（`frontends/shell/src/App.tsx`），
所以能直接登进去；而**子应用 standalone 的启动流程不读 cookie**
（`frontends/shared/shell-sdk/src/authBootstrap.ts`），localStorage 里没有 token 就
`redirectToLogin` → 落在调试入口上等于把刚到手的登录态丢掉，全程无报错，最后停在密码登录页。

实测对照（同一条 SSO 链路，只换落点）：

| 落点 | 回调后最终停在 | localStorage |
|---|---|---|
| `http://localhost:5175/` | `http://localhost:8080/login?redirect=…&appId=core-business`（密码登录页） | 只有 `jonex_locale` |
| `http://localhost/` | `http://localhost/`（系统首页，渲染出用户名） | `jonex_access_token` + `jonex_user` |

调试入口不是配错了：它服务于另一条链路——宿主登录后用 **login ticket** 换 token 跳进 standalone 子应用
（`Login/index.tsx` 的 `handleRedirect`，后端按「同 scheme+host+port、path 前缀」匹配白名单，
所以 `http://localhost:5175/` 这条正是给 `/core-business/` 用前缀放行的）。它接不住的只是**直接 SSO 回调**这一种。

**默认落点规则**：取被最多应用共用的那个；并列时（如 `:8080` 与 `:80` 同时配着）探测本机哪个在跑，
取第一个应答的。探不到就退回首位并打印警告，不会假装成功。用 `--list` 可以一次看清全部落点归属。

## 命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `-g, --gateway` | Gateway 基地址 | `http://localhost:8000`，可被环境变量 `JONEX_GATEWAY_URL` 覆盖 |
| `-t, --tenant` | 租户 ID | `tenant_jonex_demo`，可被 `JONEX_SSO_TENANT` 覆盖 |
| `-r, --redirect` | 登录成功后的前端落点 | 省略时自动选**宿主入口**（会打印选了哪个；见「落点怎么选」） |
| `--idp` | 追加 `kc_idp_hint` 直跳外部身份源，如 `casdoor` | 空（走 Keycloak 登录页） |
| `--env-file` | 指定白名单来源 env 文件 | 依次找 `deploy/.env`、`.env.local` |
| `--timeout` | HTTP 超时秒数 | `10` |
| `--no-probe` | 自动选落点时跳过可达性探测（前端不在本机跑时用） | —（默认探测，上限 3s） |
| `--list` | 只列出白名单里的落点候选，不发起请求 | — |

## 关键前提：为什么必须向后端要

**Keycloak 的 `state` 是一次性票据，由后端在发起时生成并写入 Redis。**

- 回调侧用 `getdel` 一次性消费，取不到即拒绝。
- 因此**手工拼出来的 Keycloak authorize 网址一定走不通**：它的 state 不在 Redis 里，回跳会被 `OidcStateMismatchError`（错误码 3015）拒绝。
- 本脚本产出的网址**源自后端发起端点**，state 是真实写入 Redis 的那一枚，所以可用。

### 有效期与一次性语义

- state 的 TTL 取 `KEYCLOAK_STATE_TTL_SECONDS`，**未配置时后端默认 300 秒**（`capabilities/platform/auth/auth_service.py`）。
- 逾期未用即失效，**重跑脚本拿新的即可**。
- 精确语义（实测确认）：**打开 Keycloak 登录页并不会消费 state**，同一把 state 重复打开登录页仍然正常；只有**登录回调成功一次**之后，state 才被消费掉。所以"先打开看一眼、再回来重新打开"不会把链接弄坏。

## 常见报错排查

脚本遇到后端返回错误时**不吞错误码**，会原样透出 `code` 与 `message`，并附排查提示，退出码为 1。

| code | HTTP | 含义 | 怎么办 |
|---|---|---|---|
| 3006 | 403 | 登录后落点不在 `AUTH_ALLOWED_REDIRECT_URIS` 白名单 | 跑 `--list` 挑一个合法落点 |
| 3005 | 403 | 租户非法（空 / `default` / `default_tenant` / `system` 被禁） | 换合法租户，本机 demo 用 `tenant_jonex_demo` |
| 1001 | 422 | 缺 query 参数（`tenant_id` 或 `redirect_uri`） | 检查 `-t` / `-r` 是否传全 |
| 3015 | 400 | state 缺失 / 不匹配 / **已消费**（`OidcStateMismatchError`） | 链接过期或已用过一次登录，重跑脚本 |
| 3022 | 403 | 租户 `login_policy=synced_only` 且该用户未同步（`OidcSyncRequiredError`） | **业务预期，不是网址生成错误**——走完外部身份源登录后被拒属正常行为 |

连接类错误（脚本明确提示，非上表）：Gateway 未启动或地址不对 → 确认服务在跑，或用 `-g` 指定正确地址。

### 两类「不报错但登不进去」

**① 停在密码登录页**——全程零报错，走完上游登录后落在登录页。这是**落点选成了调试入口**，
不是链路坏了，见「落点怎么选」。`-r` 显式指到这类落点时脚本会直接告警。

**② 3022 被拒**——demo 租户的 `login_policy` 若为 `synced_only` 且该用户未同步，走完 Casdoor
登录后回调返回 403 + 3022。这代表登录策略生效，不代表脚本产出的网址有问题——验收时别误判。
两者都不在错误码表里，别去查链路。

## 换环境

落点白名单、租户、Gateway 地址都不是写死的：

```bash
# 指定其它 Gateway
python3 scripts/local-dev/sso_login_url.py -g http://172.30.2.57:8000

# 指定其它 env 文件作为白名单来源（该文件里要有 AUTH_ALLOWED_REDIRECT_URIS）
python3 scripts/local-dev/sso_login_url.py --env-file deploy/.env

# 指定的 env 文件里没有白名单时，落点必须显式给出（否则无从自动选取）
python3 scripts/local-dev/sso_login_url.py --env-file my.env -r http://localhost:8080/

# 前端不在本机跑（落点探测没意义）时跳过探测
python3 scripts/local-dev/sso_login_url.py --no-probe
```

白名单从 env 文件的 `AUTH_ALLOWED_REDIRECT_URIS` 读取（JSON，形如 `{"core-business":["http://localhost:5175/","http://localhost:8080/"]}`）。找到哪一份、自动选中的落点来自哪里，脚本都会打印出来。

测试服务器的 `deploy/.env`（如 `claude_ignore/keycloak/环境文件/57.env`）中，`AUTH_ALLOWED_REDIRECT_URIS` 与本机是同一套 localhost 口径，通常只需换 `-g` 网关地址，不必换白名单。

## 设计约定

**脚本里没有 realm、没有 client_id、没有前端端口。** 这不是遗漏，是有意为之：

- `realm` 和 `client_id` 都从后端 302 响应的 `Location` 里解析得到，脚本无需也不该知道。
- 落点候选**原样读自白名单配置**，脚本不认「哪个端口对应哪个应用」，只数**这个落点被几个应用声明**——
  宿主入口（多个应用共用）与调试入口（单个应用专属）就是这么分出来的，全程由配置说了算。
  白名单增删应用或改端口时脚本自动跟随，代码一行不用改。

脚本里仅有的两个默认值是 Gateway 地址与 demo 租户 ID（见参数表），都可以用参数或环境变量覆盖——它们是「零参数即用」的前提。

**本脚本不改动任何后端 / 前端业务代码，不新增接口**，只读地调用已有的发起端点。
