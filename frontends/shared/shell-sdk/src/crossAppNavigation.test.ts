import { afterEach, describe, expect, it, vi } from 'vitest';
import { CROSS_APP_NAVIGATE_EVENT, navigateCrossApp, type CrossAppNavigateDetail } from './crossAppNavigation';

const ORIGIN = 'https://jonex.example';
const KB_PATH = '/apps/core-business/domain-knowledge/kb-1';

interface Harness {
  events: { type: string; detail?: CrossAppNavigateDetail }[];
  /** 本窗（hosted 下它就是顶层；iframe fallback 下它是被嵌的子应用） */
  selfLocation: { href: string };
  /** 顶层窗口 —— hosted 下与本窗是同一个对象，iframe fallback 下是父窗 */
  topLocation: { href: string };
  parentMessages: { data: unknown; targetOrigin: string }[];
  /** 注册一个「接管者」：收到事件后决定要不要 acknowledge（对应 shell 侧对路径的取舍） */
  listen: (fn: (detail: CrossAppNavigateDetail) => void) => void;
}

function installGlobals({ embedded = false, crossOrigin = false } = {}): Harness {
  const events: Harness['events'] = [];
  const parentMessages: Harness['parentMessages'] = [];
  const handlers: ((detail: CrossAppNavigateDetail) => void)[] = [];
  const selfLocation = { href: '', search: embedded ? '?embed=1' : '', origin: ORIGIN };
  const topLocation = { href: '' };

  class FakeCustomEvent<T = unknown> {
    detail?: T;
    constructor(
      public type: string,
      init?: { detail?: T },
    ) {
      this.detail = init?.detail;
    }
  }

  const fakeWindow: Record<string, unknown> = {
    location: selfLocation,
    dispatchEvent: (event: FakeCustomEvent<CrossAppNavigateDetail>) => {
      events.push({ type: event.type, detail: event.detail });
      // 真实 dispatchEvent 是同步调用监听者的：回执必须在返回前落定
      if (event.type === CROSS_APP_NAVIGATE_EVENT && event.detail) {
        handlers.forEach((fn) => fn(event.detail!));
      }
      return true;
    },
  };

  // 父窗是独立对象（而不是本窗），才能分辨 postMessage 发给了谁 ——
  // 真实浏览器里顶层窗口的 parent 与 top 都指向它自己
  const parentWindow = {
    location: topLocation,
    postMessage: (data: unknown, targetOrigin: string) => {
      parentMessages.push({ data, targetOrigin });
    },
  };

  if (crossOrigin) {
    Object.defineProperty(fakeWindow, 'top', {
      get() {
        throw new Error('SecurityError: cross-origin frame');
      },
    });
    fakeWindow.parent = fakeWindow;
  } else {
    fakeWindow.top = embedded ? parentWindow : fakeWindow;
    fakeWindow.parent = embedded ? parentWindow : fakeWindow;
  }

  vi.stubGlobal('window', fakeWindow);
  vi.stubGlobal('CustomEvent', FakeCustomEvent);

  return { events, selfLocation, topLocation, parentMessages, listen: (fn) => handlers.push(fn) };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('navigateCrossApp —— hosted（同窗）', () => {
  it('把目标路径交给接管者，接管后不再回落整页导航', () => {
    const harness = installGlobals();
    harness.listen((detail) => detail.acknowledge());

    expect(navigateCrossApp(KB_PATH)).toBe(true);

    expect(harness.events).toHaveLength(1);
    expect(harness.events[0].type).toBe(CROSS_APP_NAVIGATE_EVENT);
    expect(harness.events[0].detail?.path).toBe(KB_PATH);
    expect(harness.selfLocation.href).toBe('');
  });

  it('没人接管（standalone 独立端口）时回落到整页导航', () => {
    const harness = installGlobals();

    expect(navigateCrossApp(KB_PATH)).toBe(false);

    expect(harness.selfLocation.href).toBe(KB_PATH);
  });

  it('收到了但不应答（shell 判定路径不归本协议）时同样回落', () => {
    const harness = installGlobals();
    harness.listen(() => {});

    expect(navigateCrossApp('/login')).toBe(false);

    expect(harness.selfLocation.href).toBe('/login');
  });
});

describe('navigateCrossApp —— 被 shell 嵌入的 iframe', () => {
  it('改发 postMessage 给父窗，不做整页导航', () => {
    const harness = installGlobals({ embedded: true });

    expect(navigateCrossApp(KB_PATH)).toBe(true);

    expect(harness.parentMessages).toEqual([
      { data: { type: CROSS_APP_NAVIGATE_EVENT, path: KB_PATH }, targetOrigin: ORIGIN },
    ]);
    expect(harness.selfLocation.href).toBe('');
    expect(harness.topLocation.href).toBe('');
  });

  it('没有 embed 标记（被别的页面嵌了，不是 shell）时不发 postMessage，走整页导航', () => {
    const harness = installGlobals({ embedded: false });

    expect(navigateCrossApp(KB_PATH)).toBe(false);

    expect(harness.parentMessages).toEqual([]);
    expect(harness.selfLocation.href).toBe(KB_PATH);
  });

  it('跨域 iframe 下退回本窗，不把 SecurityError 抛给调用方', () => {
    const harness = installGlobals({ crossOrigin: true });

    let handled: boolean | undefined;
    expect(() => {
      handled = navigateCrossApp(KB_PATH);
    }).not.toThrow();

    expect(handled).toBe(false);
    expect(harness.selfLocation.href).toBe(KB_PATH);
  });
});
