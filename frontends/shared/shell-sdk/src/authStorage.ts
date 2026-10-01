export const JONEX_ACCESS_TOKEN_KEY = 'jonex_access_token';
export const JONEX_REFRESH_TOKEN_KEY = 'jonex_refresh_token';
export const JONEX_USER_KEY = 'jonex_user';
export const JONEX_TOKEN_EXPIRES_AT_KEY = 'jonex_token_expires_at';
export const LEGACY_USER_INFO_KEY = 'userInfo';
export const JONEX_IMPERSONATION_BACKUP_KEY = 'jonex_impersonation_backup';

export const LEGACY_AUTH_STORAGE_KEYS = ['jonex_api_key', 'jonex_token'];

function getStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage;
}

export function readAccessToken(): string | null {
  return getStorage()?.getItem(JONEX_ACCESS_TOKEN_KEY) ?? null;
}

export function writeAccessToken(token: string): void {
  if (!token) return;
  getStorage()?.setItem(JONEX_ACCESS_TOKEN_KEY, token);
}

export function readRefreshToken(): string | null {
  return getStorage()?.getItem(JONEX_REFRESH_TOKEN_KEY) ?? null;
}

export function writeRefreshToken(token?: string | null): void {
  if (!token) return;
  getStorage()?.setItem(JONEX_REFRESH_TOKEN_KEY, token);
}

/** 读取 access token 的绝对过期时间戳（ms），未记录时返回 null */
export function readTokenExpiresAt(): number | null {
  const raw = getStorage()?.getItem(JONEX_TOKEN_EXPIRES_AT_KEY);
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** 记录 access token 的绝对过期时间戳（ms）；传 null 清除 */
export function writeTokenExpiresAt(expiresAtMs: number | null): void {
  const storage = getStorage();
  if (!storage) return;
  if (expiresAtMs === null) storage.removeItem(JONEX_TOKEN_EXPIRES_AT_KEY);
  else storage.setItem(JONEX_TOKEN_EXPIRES_AT_KEY, String(expiresAtMs));
}

export function readCachedUser<T = unknown>(): T | null {
  const storage = getStorage();
  if (!storage) return null;
  let raw = storage.getItem(JONEX_USER_KEY);
  if (raw === null) {
    // 一次性迁移：历史遗留的 userInfo 旧键 → jonex_user（读后即迁并清除旧键）
    raw = storage.getItem(LEGACY_USER_INFO_KEY);
    if (raw !== null) {
      storage.setItem(JONEX_USER_KEY, raw);
      storage.removeItem(LEGACY_USER_INFO_KEY);
    }
  }
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function writeCachedUser(user: unknown): void {
  const storage = getStorage();
  if (!storage) return;
  // 统一只写 jonex_user；顺带清除历史残留的 userInfo 旧键
  storage.setItem(JONEX_USER_KEY, JSON.stringify(user));
  storage.removeItem(LEGACY_USER_INFO_KEY);
}

export function clearAuthStorage(options: { keepLocale?: boolean } = {}): void {
  const storage = getStorage();
  if (!storage) return;
  const locale = options.keepLocale ? storage.getItem('locale') : null;
  storage.removeItem(JONEX_ACCESS_TOKEN_KEY);
  storage.removeItem(JONEX_REFRESH_TOKEN_KEY);
  storage.removeItem(JONEX_TOKEN_EXPIRES_AT_KEY);
  storage.removeItem(JONEX_USER_KEY);
  storage.removeItem(LEGACY_USER_INFO_KEY);
  storage.removeItem(JONEX_IMPERSONATION_BACKUP_KEY);
  LEGACY_AUTH_STORAGE_KEYS.forEach((key) => storage.removeItem(key));
  if (options.keepLocale && locale) storage.setItem('locale', locale);
}

export interface ImpersonationAuthBackup {
  accessToken: string | null;
  refreshToken: string | null;
}

export function backupAuthForImpersonation(): void {
  const storage = getStorage();
  if (!storage) return;
  const backup: ImpersonationAuthBackup = {
    accessToken: storage.getItem(JONEX_ACCESS_TOKEN_KEY),
    refreshToken: storage.getItem(JONEX_REFRESH_TOKEN_KEY),
  };
  storage.setItem(JONEX_IMPERSONATION_BACKUP_KEY, JSON.stringify(backup));
  // 模拟态只剩 access token，没有任何可续期的 refresh 凭证：
  // ① refresh token 已迁到 httpOnly cookie（SSO PRD §5.4/BC4），JS 读不到也删不掉，
  //    这一行 removeItem 删的是**空键**，真 cookie 一个字节没碰；
  // ② create_impersonation_token() 从不签发 refresh。
  // 所以「refresh 端点天然拒绝 type=user」这个旧承诺从未落实 —— 真正的防线是
  // callRefresh() 里的模拟态短路（shell-sdk/autoRefresh.ts）+ 后端的标记 cookie。
  storage.removeItem(JONEX_REFRESH_TOKEN_KEY);
  // 必须在 reload 前清掉原租户残留的 expires_at：否则 scheduleAutoRefresh() 会判定
  // 「即将过期」并在 reload 后**立刻**触发一次 refresh，用原租户 cookie 换回原租户
  // token 覆盖模拟 token（跨租户缺陷的加速器）。
  writeTokenExpiresAt(null);
}

export function restoreAuthFromImpersonation(): void {
  const storage = getStorage();
  if (!storage) return;
  const raw = storage.getItem(JONEX_IMPERSONATION_BACKUP_KEY);
  if (raw === null) return; // 幂等：无备份视为已恢复
  try {
    const backup = JSON.parse(raw) as ImpersonationAuthBackup;
    if (backup.accessToken) storage.setItem(JONEX_ACCESS_TOKEN_KEY, backup.accessToken);
    if (backup.refreshToken) storage.setItem(JONEX_REFRESH_TOKEN_KEY, backup.refreshToken);
  } catch {
    // 忽略损坏的备份
  }
  storage.removeItem(JONEX_IMPERSONATION_BACKUP_KEY);
}
