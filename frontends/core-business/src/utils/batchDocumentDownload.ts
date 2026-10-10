/**
 * 批量触发文档下载：逐个拉取下载 URL，用隐藏 <a> 触发浏览器下载。
 * 相邻两次触发间隔 delayMs（默认 300ms），降低浏览器多下载拦截概率。
 */

export interface BatchDownloadOptions {
  /** 拉取单个文档下载 URL（返回 /raw?token=... 形式） */
  fetchTicket: (docId: string) => Promise<{ url: string }>;
  /** 完成进度回调（done: 已处理数, total: 总数） */
  onProgress?: (done: number, total: number) => void;
  /** 触发间隔（毫秒），默认 300；测试传 0 跳过等待 */
  delayMs?: number;
  /** 创建下载链接，默认 document.createElement('a')；测试注入用 */
  createLink?: (url: string) => { click: () => void };
}

export interface BatchDownloadResult {
  ok: number;
  failed: number;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 触发单个文件下载：创建隐藏 <a> 并点击。
 *
 * 刻意用 <a> 而不是 fetch + createObjectURL：COS/S3 预签名是跨域直连，
 * fetch 需要 bucket 的 CORS 放行前端 origin（<video>/<img> 走 no-cors 不需要），
 * 而 <a> 导航由 Content-Disposition: attachment 承载文件名，无跨域要求。
 * 同源（local 后端 / gateway proxy）与 302 直连对象存储两种形态都适用。
 */
export function clickDownloadLink(url: string): void {
  const a = document.createElement('a');
  a.href = url;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function defaultCreateLink(url: string): { click: () => void } {
  const a = document.createElement('a');
  a.href = url;
  a.style.display = 'none';
  document.body.appendChild(a);
  return {
    click: () => {
      a.click();
      a.remove();
    },
  };
}

export async function batchDownloadDocuments(
  docIds: string[],
  options: BatchDownloadOptions,
): Promise<BatchDownloadResult> {
  const { fetchTicket, onProgress, delayMs = 300, createLink } = options;
  let ok = 0;
  let failed = 0;
  const total = docIds.length;
  for (let i = 0; i < docIds.length; i += 1) {
    try {
      const { url } = await fetchTicket(docIds[i]);
      const link = createLink ? createLink(`${url}&download=1`) : defaultCreateLink(`${url}&download=1`);
      link.click();
      ok += 1;
    } catch {
      failed += 1;
    }
    onProgress?.(i + 1, total);
    if (delayMs > 0 && i < docIds.length - 1) {
      await sleep(delayMs);
    }
  }
  return { ok, failed };
}
