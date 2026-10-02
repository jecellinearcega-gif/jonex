/**
 * 数据运营模块的纯函数工具。
 *
 * 数字串一律经这三个 format 产出，各处不再各写一遍 toLocaleString 与小数位 ——
 * 常量层与取数层都从这里取，接后端后数字换成真实值，格式化仍然只有这一条路。
 */

export const fmtCount = (n: number) => n.toLocaleString('en-US');
export const fmtRate = (n: number) => `${n.toFixed(2)}%`;
export const fmtSigned = (n: number) => `+${n.toLocaleString('en-US')}`;

/* ══════════════════════ tab 值 ══════════════════════ */

/**
 * 数据运营的三个 tab 是同一页面的三块内容，tab 值落在 URL query 上。
 * 取值校验放这里，避免散在页面与 tab 栏两处。
 */
export type DoTabKey = 'assets' | 'knowledge' | 'search';

/** 顺序即 tab 栏顺序 */
export const DO_TAB_KEYS: readonly DoTabKey[] = ['assets', 'knowledge', 'search'];

/** 非法或缺省的 query 一律回落第一个 tab，避免手改 URL 后白屏 */
export function toDoTabKey(value: string | null): DoTabKey {
  return DO_TAB_KEYS.includes(value as DoTabKey) ? (value as DoTabKey) : DO_TAB_KEYS[0];
}
