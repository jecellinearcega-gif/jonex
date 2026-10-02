import apiClient from './request';

/**
 * 平台级租户权限码（与后端 `require_permission` 的入参保持一致）。
 *
 * - read：租户列表 / 详情 / 用户数统计
 * - write：创建 / 编辑 / 删除租户
 */
export const TENANT_READ_PERMISSION = 'platform:tenant:read';
export const TENANT_WRITE_PERMISSION = 'platform:tenant:write';

/** 后端列表 `page_size` 上限；搜索走单页拉取时用它兜住匹配范围 */
export const TENANT_PAGE_SIZE_MAX = 100;

export interface TenantListResponse {
  items: TenantItem[];
  total: number;
}

export interface TenantItem {
  id: string;
  name: string;
  description: string | null;
  status: number;
  plan_type: string;
  expire_time: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface TenantCreatePayload {
  id: string;
  name: string;
  description?: string;
  plan_type?: string;
  /** 创建时即可设置到期时间；后端不接收初始 status（走库默认值） */
  expire_time?: string | null;
}

export interface TenantUpdatePayload {
  name?: string;
  description?: string;
  plan_type?: string;
  status?: number;
  /** 后端 partial update 语义：显式传 null 才会清空，不传则保持原值 */
  expire_time?: string | null;
}

export async function listTenants(page = 1, pageSize = 100): Promise<TenantListResponse> {
  return apiClient.get<TenantListResponse>('/platform/tenants', {
    params: { page, page_size: pageSize },
  });
}

/** 单个租户详情（刷新 / 深链 / 编辑后回读的唯一数据源） */
export async function getTenant(id: string): Promise<TenantItem> {
  return apiClient.get<TenantItem>(`/platform/tenants/${encodeURIComponent(id)}`);
}

export async function createTenant(data: TenantCreatePayload): Promise<TenantItem> {
  return apiClient.post<TenantItem>('/platform/tenants', data);
}

export async function updateTenant(id: string, data: TenantUpdatePayload): Promise<TenantItem> {
  return apiClient.patch<TenantItem>(`/platform/tenants/${id}`, data);
}

export async function deleteTenant(id: string): Promise<void> {
  await apiClient.delete<null>(`/platform/tenants/${id}`);
}

export async function getTenantUserCounts(): Promise<Record<string, number>> {
  return apiClient.get<Record<string, number>>('/platform/tenants/user-counts');
}

export function getStatusLabel(t: (key: string) => string, status: number): { label: string; color: string } {
  return status === 1
    ? { label: t('status.enabled'), color: 'success' }
    : { label: t('status.disabled'), color: 'warning' };
}
