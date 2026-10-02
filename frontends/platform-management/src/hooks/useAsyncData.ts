import { useEffect, useState } from 'react';

/**
 * 按 load 的引用变化重新取数，并在取数期间给出 loading。
 *
 * load 必须是用 useCallback 包过的稳定引用，否则每次渲染都会重新取一遍。
 * 用 cancelled 丢弃过期响应：快速连点切换时，旧请求的结果可能晚于新请求到达。
 */
export default function useAsyncData<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch(() => {
        // 取数失败退到空态：卡片各自空着，不把整页打成错误页
        if (!cancelled) setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  return { data, loading };
}
