import { useEffect, useRef, useState } from 'react';

/**
 * 量宿主容器的可用高度，并随容器尺寸变化同步。
 *
 * antd 的固定表头只认具体像素高度（scroll.y），不能用 100%。而 calc(100vh - N) 有两个坑：
 * hosted 下子应用跑在 shell 的 iframe 里，100vh 是 iframe 高度，比 standalone 多算一层顶栏；
 * 同一个视图在文档级、知识库级两处嵌入时上面的东西也不一样。量真实容器就没有这些前提。
 *
 * 返回值只是 max-height 上限，实际高度由 CSS 的 flex 分配决定——所以给容器全高即可，
 * 不必预先减去表头与分页的高度。
 */
export default function useFillHeight(min = 200) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(400);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const sync = () => setHeight(Math.max(host.clientHeight, min));
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(host);
    return () => observer.disconnect();
  }, [min]);

  return [hostRef, height] as const;
}
