import apiClient from './request';

/**
 * 企业身份集成 API（Keycloak SSO + 用户同步）。
 *
 * 契约来源：后端 `capabilities/platform/dtos/identity_integration.py`（E1–E6 单一契约源），
 * 经 Gateway REST 透传 `/api/v1/platform/identity-integration**` → Sidecar → platform。
 *
 * 响应已由 apiClient 解包信封（`code !== 0` 会 reject，错误对象带 `bizCode` / `status`），
 * 故此处直接返回 `data` 结构。
 *
 * 几个必须知道的语义（与移交文档一致，已对照后端实现核实）：
 * - `token` 是**只写字段**：后端恒回显 `***`。未修改就不传该字段，
 *   传 `***` 等同于不覆盖（见 `SECRET_MASK`）。
 * - SSO 的 Client Secret 由部署侧注入（平台级单 client，见后端 `KEYCLOAK_CLIENT_SECRET`），
 *   不进本契约：`SsoConfigUpdate` / `SsoConfigResponse` 均无 `client_secret` 字段。
 * - E1 的 `latest_sync_record` / `datasource.last_sync_at` / `next_sync_at` 一期恒为 `null`
 *   （自动调度未落地），最近同步记录一律改走 E5 列表。
 * - E4「立即同步」在请求内同步执行，返回的 `status` 是**终态**；且连接/认证/契约失败
 *   不会以错误码抛出，而是任务落 `failed` 并正常返回 —— 调用方必须检查 `status`。
 * - E5 的 `user_change` 是**数字标量**（新增+更新+停用之和），E6 的同名字段是**对象**。
 */

/** 端点前缀（apiClient baseURL 已含 /api/v1） */
const BASE = '/platform/identity-integration';

/** 敏感字段回显占位：后端恒返回 `***`，前端不得用它覆盖原值（未修改就不传该字段） */
export const SECRET_MASK = '***';

/** 同步周期可选项（后端仅存正整数，无单位语义；单位口径待产品确认，一期沿用三档下拉） */
export const INTERVAL_OPTIONS: ReadonlyArray<{ value: number; labelKey: string }> = [
  { value: 6, labelKey: 'cycleOpt6h' },
  { value: 12, labelKey: 'cycleOpt12h' },
  { value: 24, labelKey: 'cycleOpt24h' },
];

/** 登录字段映射：三条固定映射，value 为后端 users 列名（sub 固定唯一标识不可编辑、不在映射内） */
export const LOGIN_FIELD_MAPPING: ReadonlyArray<{ key: string; value: string; labelKey: string }> = [
  { key: 'preferred_username', value: 'username', labelKey: 'fieldUsername' },
  { key: 'name', value: 'display_name', labelKey: 'fieldDisplayName' },
  { key: 'email', value: 'email', labelKey: 'fieldEmail' },
];

// ============ 枚举值字典 ============

export type LoginPolicy = 'auto_create' | 'synced_only';
export type SyncType = 'incremental' | 'full';
export type SyncTaskStatus = 'pending' | 'running' | 'success' | 'partial_failed' | 'failed' | 'canceled';
export type SyncTrigger = 'manual' | 'auto';
export type ConnectionStatus = 'unconfigured' | 'verified' | 'unverified';
export type SyncTarget = 'sso' | 'datasource';

// ============ E1 查询统一展示 ============

export interface SsoStatus {
  enabled: boolean;
  /** 一期固定 'keycloak' */
  provider: string;
  login_policy: LoginPolicy;
  last_verified_at: string | null;
}

export interface DatasourceStatus {
  name: string | null;
  /** 派生字段（后端按 url + verified_at 推导），非存储枚举 */
  connection_status: ConnectionStatus;
  auto_sync: boolean;
  /** 同步周期（整数，单位待产品确认） */
  interval: number | null;
  /** ⚠️ 一期恒 null（自动调度未落地） */
  last_sync_at: string | null;
  /** ⚠️ 一期恒 null */
  next_sync_at: string | null;
}

export interface SyncRecordSummary {
  /** 同步任务主键（= E6 详情端点 path 的 record_id），列表行据此打开详情 */
  id: number;
  sync_time: string | null;
  status: SyncTaskStatus;
  /** 聚合标量 = added + updated + disabled + enabled + deleted */
  user_change: number;
  failed_count: number;
  trigger: SyncTrigger;
}

export interface IntegrationStatusResponse {
  sso: SsoStatus;
  datasource: DatasourceStatus;
  /** ⚠️ 一期恒 null，最近同步记录改走 E5 */
  latest_sync_record: SyncRecordSummary | null;
}

// ============ E2 更新配置 ============

export interface SsoConfigUpdate {
  enabled?: boolean;
  login_policy?: LoginPolicy;
  /**
   * Realm 服务地址（`tenant_realm_map.issuer_url`），抽屉里唯一可改的 Realm 参数。
   *
   * **不传 = 保持原值**，且空串会被后端判非法（1007），所以「清空」不是有效操作，
   * 前端拿不准时一律不传该字段。
   *
   * 语义提醒：后端登录链路读的是部署侧环境变量（`KEYCLOAK_ISSUER_WHITELIST` /
   * `KEYCLOAK_SERVER_URL`），租户反查只按 `realm` 列匹配，**都不读本字段** ——
   * 它只是「测试连接」的出站探测目标，改它不会改变登录链路。
   */
  issuer_url?: string;
}

export interface DatasourceConfigUpdate {
  name?: string;
  /** 接口地址，http/https 均可，允许私网/回环（后端做 SSRF 校验，scheme 非法或缺主机名 → 1007） */
  url?: string;
  auth_type?: string;
  /** 未改不传；传 `***` 不覆盖原值 */
  token?: string;
  auto_sync?: boolean;
  /** 同步周期，正整数（≤0 由后端 DTO 校验拦下，返回 422） */
  interval?: number;
}

export interface IntegrationConfigUpdateRequest {
  sso?: SsoConfigUpdate;
  datasource?: DatasourceConfigUpdate;
}

export interface SsoConfigResponse {
  enabled: boolean;
  login_policy: LoginPolicy;
  last_verified_at: string | null;
}

export interface DatasourceConfigResponse {
  name: string | null;
  url: string | null;
  auth_type: string | null;
  /** 恒 `***` */
  token: string;
  auto_sync: boolean;
  interval: number | null;
  last_verified_at: string | null;
}

export interface IntegrationConfigResponse {
  sso: SsoConfigResponse;
  datasource: DatasourceConfigResponse;
}

// ============ E3 测试连接 ============

export interface TestConnectionRequest {
  target: SyncTarget;
  sso?: SsoConfigUpdate;
  datasource?: DatasourceConfigUpdate;
}

export interface TestConnectionResponse {
  ok: boolean;
  /** 出站探测 `{issuer_url}/.well-known/openid-configuration` 的真实耗时；仅成功路径有值 */
  latency_ms: number | null;
  message: string | null;
}

// ============ E4 立即同步 ============

export interface SyncRequest {
  mode: SyncType;
  /**
   * 绕过「未通过连接测试」闸门（后端 `DataSourceNotVerifiedError` 5008 / HTTP 409）。
   *
   * 只在手动同步的二次确认之后才传 `true`（甲案：按钮保持可点，由
   * `SyncNowModal` 用红字警告换取用户显式确认）；不传 = 默认被闸门拦下。
   */
  force?: boolean;
}

export interface SyncResponse {
  task_id: number;
  /** ⚠️ 同步执行，此处为终态：success / partial_failed / failed */
  status: SyncTaskStatus;
}

// ============ E5 同步记录列表 ============

export interface SyncRecordListResponse {
  items: SyncRecordSummary[];
  total: number;
}

// ============ E6 同步记录详情 ============

export interface UserChangeCounts {
  added: number;
  updated: number;
  disabled: number;
  enabled: number;
  /** 上游确认该账号已被删除、平台将其软删 */
  deleted: number;
}

export interface SyncFailureDetail {
  /** 一期仅 'user' */
  entity_type: string;
  external_entity_id: string | null;
  reason: string | null;
}

export interface SyncRecordDetailResponse {
  sync_time: string | null;
  sync_type: SyncType;
  status: SyncTaskStatus;
  failed_count: number;
  /** ⚠️ 与 E5 同名不同型：此处为对象 */
  user_change: UserChangeCounts;
  failures: SyncFailureDetail[];
}

// ============ SSO Realm 只读展示 ============

export interface SsoRealmResponse {
  /** 由 tenant_id 推导（jonex- 前缀），恒有值 */
  realm: string;
  /** tenant_realm_map.issuer_url，未配置时为 null */
  issuer_url: string | null;
  /** tenant_realm_map.client_id，未配置时为 null */
  client_id: string | null;
  /** 来自配置项 KEYCLOAK_REDIRECT_URI，未配置时为 null */
  redirect_uri: string | null;
  /** tenant_realm_map.status：0 停用 / 1 启用 */
  status: number;
  /**
   * 「测试连接」实际会探测的完整 URL（后端按 白名单反查 → KEYCLOAK_SERVER_URL → issuer_url
   * 三级解析后得出）。与 `issuer_url` 可能不同 —— 后者是浏览器视角的 OIDC 标识，前者是
   * 容器内可出站的地址。后端无法解析出目标时为 null。
   */
  effective_probe_target: string | null;
}

// ============ 请求函数 ============

/** E1 查询统一展示（SSO 状态 + 主数据源状态） */
export async function getIntegrationStatus(): Promise<IntegrationStatusResponse> {
  return apiClient.get<IntegrationStatusResponse>(BASE);
}

/** E2 更新配置（部分更新，未传字段保持原值） */
export async function updateIntegrationConfig(
  data: IntegrationConfigUpdateRequest,
): Promise<IntegrationConfigResponse> {
  return apiClient.put<IntegrationConfigResponse>(BASE, data);
}

/** E3 测试连接（target=sso 或 datasource） */
export async function testConnection(data: TestConnectionRequest): Promise<TestConnectionResponse> {
  return apiClient.post<TestConnectionResponse>(`${BASE}/test-connection`, data);
}

/**
 * E4 立即同步（mode=incremental/full），返回终态。
 *
 * `force` 为波 3 新增的可选末位参数（默认 `false`）：未通过连接测试时后端返回 5008，
 * 只有用户在弹窗里显式确认「仍要同步」才传 `true`。**默认值保证既有调用语义不变**。
 */
export async function triggerSync(mode: SyncType = 'incremental', force = false): Promise<SyncResponse> {
  const body: SyncRequest = force ? { mode, force } : { mode };
  return apiClient.post<SyncResponse>(`${BASE}/sync`, body);
}

/** E5 同步记录列表（分页） */
export async function listSyncRecords(page = 1, pageSize = 20): Promise<SyncRecordListResponse> {
  return apiClient.get<SyncRecordListResponse>(`${BASE}/sync-records`, {
    params: { page, page_size: pageSize },
  });
}

/** E6 同步记录详情（含失败明细） */
export async function getSyncRecord(recordId: number): Promise<SyncRecordDetailResponse> {
  return apiClient.get<SyncRecordDetailResponse>(`${BASE}/sync-records/${recordId}`);
}

/**
 * 「该租户尚未开通 SSO」的统一判定 —— realm 查询与测试连接两条路径共用。
 *
 * 后端已把这条前置条件统一成 1002（ResourceNotFoundError，HTTP 404）；仍留着对
 * 裸 404 的兼容，避免中间层改写状态码时把正常分支误判成故障。
 */
export function isSsoNotProvisioned(err: unknown): boolean {
  const e = err as { status?: number; bizCode?: number } | null;
  return e?.status === 404 || e?.bizCode === 1002;
}

/**
 * 取出后端回报的「实际探测目标」（`errorDetails.target`，完整 probe_url）。
 *
 * SSO 与同步两条测试连接路径共用（错误对象上的 `errorDetails` 由请求层填好）。
 *
 * 只认非空字符串：该字段是后端后续新增的，未部署时整个 `errorDetails` 都不存在，
 * 前端必须回落到中性文案 —— 这里返回 null 即可，绝不能把 undefined / 对象 / 空串
 * 拼进提示里（会渲染成 `undefined` 或 `[object Object]`）。
 */
export function readProbeTarget(err: unknown): string | null {
  const e = err as { errorDetails?: Record<string, unknown> } | null | undefined;
  const target = e?.errorDetails?.target;
  if (typeof target !== 'string') return null;
  const trimmed = target.trim();
  return trimmed || null;
}

/** SSO Realm 只读展示；租户未开通时返回 1002，用 isSsoNotProvisioned 判定 */
export async function getSsoRealm(): Promise<SsoRealmResponse> {
  return apiClient.get<SsoRealmResponse>(`${BASE}/sso-realm`);
}

// ============ 展示映射 ============

/** Typography.Text 的语义色（与列表页既有风格一致，不引 Tag） */
export type StatusTextType = 'success' | 'warning' | 'danger' | 'secondary';

export interface StatusMeta {
  label: string;
  type: StatusTextType;
}

/** 仅取 i18next `t` 所需的最小签名，避免与 TFunction 重载耦合（与 tenants.ts 同风格） */
type TFn = (key: string) => string;

/** 同步状态 → 文案与语义色（success 绿 / partial_failed 橙 / failed 红 / running 蓝→secondary / 其余灰） */
export function getSyncStatusMeta(t: TFn, status: SyncTaskStatus): StatusMeta {
  const map: Record<SyncTaskStatus, StatusMeta> = {
    pending: { label: t('tenantManagement.identity.statusPending'), type: 'secondary' },
    running: { label: t('tenantManagement.identity.statusRunning'), type: 'secondary' },
    success: { label: t('tenantManagement.identity.statusSuccess'), type: 'success' },
    partial_failed: { label: t('tenantManagement.identity.statusPartialFailed'), type: 'warning' },
    failed: { label: t('tenantManagement.identity.statusFailed'), type: 'danger' },
    canceled: { label: t('tenantManagement.identity.statusCanceled'), type: 'secondary' },
  };
  return map[status] ?? { label: status, type: 'secondary' };
}

/** 主数据源连接状态（派生三态） */
export function getConnectionStatusMeta(t: TFn, status: ConnectionStatus): StatusMeta {
  const map: Record<ConnectionStatus, StatusMeta> = {
    unconfigured: { label: t('tenantManagement.identity.connUnconfigured'), type: 'secondary' },
    verified: { label: t('tenantManagement.identity.connVerified'), type: 'success' },
    unverified: { label: t('tenantManagement.identity.connUnverified'), type: 'warning' },
  };
  return map[status] ?? { label: status, type: 'secondary' };
}

/** 同步模式（增量 / 全量） */
export function getSyncTypeLabel(t: TFn, syncType: SyncType): string {
  return syncType === 'full' ? t('tenantManagement.identity.modeFull') : t('tenantManagement.identity.modeIncremental');
}

/** 触发方式（一期实际仅 manual） */
export function getTriggerLabel(t: TFn, trigger: SyncTrigger): string {
  return trigger === 'auto' ? t('tenantManagement.identity.triggerAuto') : t('tenantManagement.identity.triggerManual');
}

/** 首次登录账号策略 */
export function getLoginPolicyLabel(t: TFn, policy: LoginPolicy): string {
  return policy === 'synced_only'
    ? t('tenantManagement.identity.policySyncedOnly')
    : t('tenantManagement.identity.policyAutoCreate');
}

/** 同步周期（整数 → 文案；后端无单位语义，一期按小时档位展示） */
export function getIntervalLabel(t: TFn, interval: number | null): string {
  if (interval == null) return '';
  const option = INTERVAL_OPTIONS.find((item) => item.value === interval);
  return option ? t(`tenantManagement.syncDrawer.${option.labelKey}`) : String(interval);
}

/** E6 失败明细的实体类型（一期仅 user） */
export function getEntityTypeLabel(t: TFn, entityType: string): string {
  return entityType === 'user' ? t('tenantManagement.identity.entityUser') : entityType;
}
