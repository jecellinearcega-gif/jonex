/**
 * 平台管理页面的时间展示格式。
 *
 * 后端返回无时区的 ISO 串（如 `2026-09-10T10:00:00`），`new Date` 按浏览器本地时区解析，
 * 此处只做格式化，不做时区换算。
 */
const pad2 = (n: number): string => String(n).padStart(2, '0');

/** YYYY-MM-DD HH:mm；空值返回空串，解析失败时原样回显便于排查 */
export const fmtDateTime = (iso?: string | null): string => {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};

/** YYYY-MM-DD */
export const fmtDate = (iso?: string | null): string => {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
};
