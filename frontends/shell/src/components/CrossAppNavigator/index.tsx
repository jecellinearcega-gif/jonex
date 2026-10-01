import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { CROSS_APP_NAVIGATE_EVENT, type CrossAppNavigateDetail } from '@jonex/shell-sdk';

/**
 * 跨应用导航桥：接住子应用发来的跨应用跳转请求，用 SPA 路由跳过去，外层文档不重载。
 *
 * 必须挂在 BrowserRouter 之内（useNavigate 的前提）。两条通道对应 shell 的两种挂载形态：
 * - 同窗事件（hosted / Module Federation）：同步派发，接管时当场 acknowledge，调用方据此
 *   判断「有没有人接」，接不上的会自己回落到整页导航；
 * - postMessage（iframe fallback）：子应用在另一份文档里，事件过不来。
 */
export default function CrossAppNavigator() {
  const navigate = useNavigate();

  useEffect(() => {
    // 只接管跨应用跳转：/apps/<appId>/... 之外的路径（登录页、首页）不属于本协议，
    // 不 acknowledge，交回调用方整页导航
    const go = (path: unknown): boolean => {
      if (typeof path !== 'string' || !path.startsWith('/apps/')) return false;
      navigate(path);
      return true;
    };

    const onEvent = (event: Event) => {
      const detail = (event as CustomEvent<CrossAppNavigateDetail>).detail;
      if (detail?.path && go(detail.path)) detail.acknowledge();
    };

    const onMessage = (event: MessageEvent) => {
      // 同源校验：iframe fallback 下子应用与 shell 同源，其他来源的 message 一律不认
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; path?: string } | null;
      if (data?.type !== CROSS_APP_NAVIGATE_EVENT) return;
      go(data.path);
    };

    window.addEventListener(CROSS_APP_NAVIGATE_EVENT, onEvent);
    window.addEventListener('message', onMessage);
    return () => {
      window.removeEventListener(CROSS_APP_NAVIGATE_EVENT, onEvent);
      window.removeEventListener('message', onMessage);
    };
  }, [navigate]);

  return null;
}
