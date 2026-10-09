# identity-sync-gateway · 身份同步侧车

「Jonex标准拉取 API 契约」⇄ **Casdoor Admin API** 之间的翻译官。

## 为什么需要它

平台侧 `capabilities/platform/services/identity_sync_client.py` 只会打：

```
GET {base}/users?sync_type=full|incremental[&cursor=X]
```

而真实上游（Casdoor 门户）**不实现**这个契约 —— `datasource_url` 直接指过去只会
拿到 SPA HTML，同步必然抛 `DataSourceContractError`(5007)、任务落 `failed`。

本侧车对外讲Jonex契约、对内调 Casdoor Admin API，是二者之间唯一的适配层。
平台按 **compose 服务名**寻址：

```
http://identity-sync-gateway:8099
```

⛔ 不要用 `host.docker.internal` —— 多一跳 docker0 + 宿主防火墙，且网关 IP 随
Docker 安装漂移。

## 端点

| 方法 | 路径 | 响应 |
| --- | --- | --- |
| GET | `/health` | `200 {"status":"ok"}`（不鉴权，供 healthcheck） |
| GET | `/users?sync_type=full` | `200` + 契约信封 |
| GET | `/users?sync_type=incremental[&cursor=<b64url>]` | `200` + 契约信封（带 `cursor`） |
| * | 其余路径 / 其余方法 | `404` |

错误码约定（**故意不用 400/500 表达鉴权失败**，那会被平台误分类）：

| 场景 | 状态码 | 平台侧归类 |
| --- | --- | --- |
| 缺 / 错 Bearer | `401` | `DataSourceAuthError`(5006) |
| `sync_type` 非法或缺失 | `400` | `DataSourceContractError`(5007) |
| 上游不可达 / 非 ok / 非 JSON | `502` | `DataSourceConnectionError`(5005) |
| 未预期异常 | `500` | `DataSourceConnectionError`(5005) |

### 信封

```json
{
  "users": [
    {"external_user_id": "…", "username": "…", "name": "…", "email": "…", "status": "active"}
  ],
  "has_more": false,
  "next_cursor": null,
  "cursor": "eyJ0IjoxNzU5…"
}
```

- `has_more` **恒 false**（本实现单页返回，不分页）
- `next_cursor` **恒 null**（只用于翻页，本实现用不到）
- ⚠️ **持久化游标是顶层 `cursor`，不是 `next_cursor`** —— 后者只用于翻页。回错键名
  平台会存下 `null` ⇒ 每轮从头拉、**增量静默退化成全量且不报错**。
- `cursor` 只在 `sync_type=incremental` 时出现；`full` 模式**不带这个键**

### 增量语义

Casdoor `/api/get-users` 没有 filtering 参数，只能按 `updatedTime` **时间窗近似**：

- 游标 = base64url 不透明串，解出 `{"t": <epoch 秒>}`（平台只存只回传，格式可自由演进）
- 请求不带 `cursor` ⇒ 回全量
- 新游标 = **请求开始时刻 − 2 秒**（overlap：覆盖「上次同步完成」与「本次请求开始」
  之间的写入竞态；代价只是少量重复，平台侧 upsert 幂等）
- `isDeleted` 软删条目不返回
- `updatedTime` 缺失 / 无法解析的行**保留**（宁可多同步一条，也不静默漏人）
- 游标无法解析（非 base64 / 非 JSON / 缺 `t`）⇒ WARNING + **回退全量**（全量是超集，安全）

## 配置

值一律写进 **`deploy/.env`**（已 gitignore，compose 自动读项目目录 `.env`）；
compose 的 service 块用 `${VAR:-}` 逐键引用。模板见 **`deploy/.env.example`**
的「本地联调：身份同步侧车」一组 —— 本目录**不再自带** `.env.example`：
同一个服务的配置必须成套住在**一个**文件里，拆两份必然漂移。

⚠️ 铁律：`.env` 里已定义的键，compose 的 `${VAR:-默认}` **永不生效** ——
**别改 compose 的 fallback，那行是死代码**。

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DATASOURCE_GATEWAY_TOKEN` | ✅ | — | 须与平台租户 UI 里填的 token **同值**；缺失 → 启动即退出（非 0 码） |
| `DATASOURCE_GATEWAY_PORT` | | `8099` | 容器内监听端口 |
| `DATASOURCE_GATEWAY_INCREMENTAL_MODE` | | `time_window` | `time_window` \| `always_full` |
| `MOCK_DATASOURCE_BASE_URL` | ✅ | — | 上游 IdP 基址（沿用平台既有键名） |
| `MOCK_DATASOURCE_OWNER` | ✅ | — | Casdoor org，⛔ 不是 `built-in` |
| `MOCK_DATASOURCE_ADMIN_USER` | ✅ | — | 管理账号 |
| `MOCK_DATASOURCE_ADMIN_PASSWORD` | ✅ | — | 管理口令 |
| `MOCK_DATASOURCE_PAGE_SIZE` | | `100` | 上游分页大小 |
| `MOCK_DATASOURCE_USERNAMES` | | 空=全量 | 逗号分隔登录名白名单 |
| `LOG_LEVEL` | | `INFO` | 日志级别 |

生成 token：

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(32))"
```

## 启动

```bash
# 起（profile 单独控制，默认不随 make up 起）
docker compose --profile identity-sync-gateway up -d
docker compose logs -f identity-sync-gateway

# 停
docker compose --profile identity-sync-gateway down
```

平台侧把租户的 `datasource_url` 指向 `http://identity-sync-gateway:8099`，
token 填 `DATASOURCE_GATEWAY_TOKEN` 的值。

## 验证

容器在跑时的三条冒烟（宿主机执行；端口只绑在 `127.0.0.1`）：

```bash
TOKEN=<DATASOURCE_GATEWAY_TOKEN 的值>

# ① health：200 {"status":"ok"}
curl -sS -i http://127.0.0.1:8099/health

# ② 无 token：401
curl -sS -i 'http://127.0.0.1:8099/users?sync_type=full'

# ③ 带 token：200 + 信封（users / has_more / next_cursor）
curl -sS -i -H "Authorization: Bearer $TOKEN" \
     'http://127.0.0.1:8099/users?sync_type=full'

# ④ 增量：回带顶层 cursor
curl -sS -H "Authorization: Bearer $TOKEN" \
     'http://127.0.0.1:8099/users?sync_type=incremental'
```

本地不起容器也能跑（凭据走进程环境变量，`--env-file` 找不到就当空）：

```bash
DATASOURCE_GATEWAY_TOKEN=devtoken MOCK_DATASOURCE_BASE_URL=http://127.0.0.1:8000 \
MOCK_DATASOURCE_OWNER=jonex-demo MOCK_DATASOURCE_ADMIN_USER=admin \
MOCK_DATASOURCE_ADMIN_PASSWORD=xxx \
python3 deploy/identity-sync-gateway/gateway.py --port 8099
```

单测（离线，monkeypatch 掉上游）：

```bash
.venv/bin/python -m pytest tests/unit/test_identity_sync_gateway.py -q
```

## 坑位

1. **⚠️ 上游用户会被真实建成本地账号**，且同步路径**不受 `login_policy` 约束**
   （那只管 JIT 首登）。首次接入务必用 `MOCK_DATASOURCE_USERNAMES` 收窄到 2-3 人。
2. **`full` 模式的副作用**：本地有、上游没返回的用户会被逐条记成「待确认」
   ⇒ 整次判 `partial_failed`。这是设计行为，不是故障。
3. **`owner` 不能填 `built-in`** —— 读 built-in 会返回 Unauthorized operation。
4. **`DATASOURCE_GATEWAY_TOKEN` 不是上游口令**：它只挡平台 → 侧车这一跳；
   Casdoor 的管理员口令走 `MOCK_DATASOURCE_ADMIN_PASSWORD`，两者互不相干。
5. **端口只绑 `127.0.0.1`**（compose 里是 `127.0.0.1:8099:8099`）：平台走 compose
   内网按服务名寻址，宿主机映射只为本地调试；要远程调试请用 SSH 隧道。
6. **字段映射是白名单式的**：`_to_contract()` 逐字段构造新 dict，Casdoor 用户对象里的
   `passwordSalt` / `passwordType` **绝不**会出现响应里（有单测守着）。
7. **所有请求路径的失败都是 5xx 响应，不是 `raise SystemExit`** —— 后者会杀进程。
   更早的实现用的是 `SystemExit`，一旦上游抖动整个侧车就没了。
8. **session cookie 有缓存**（TTL 600s，收到 401/403 自动重登一次）—— 桩是每请求登录。
9. **根路径 `GET /` 返回 404**：连接测试（波 2）走 `probe_contract()`，打的是
   `/users?sync_type=full`，不探根路径。
10. **别把 `identity-sync-gateway` 加进 `make up` 的默认画像**：它带 `profiles:`，
    刻意不随默认 `up` 起 —— 它需要真实的 `MOCK_DATASOURCE_*` 凭据才跑得起来。
