import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

/**
 * logoutNavigation 的单测。
 *
 * 本文件的**核心用例**是「跨文档存活」：主动登出标记（LOGOUT_SETTLE_KEY）必须能扛过
 * 「跳去 Keycloak → 302 转回」这段旅程 —— 转回时浏览器加载的是**全新文档**（新 window
 * 对象），但 sessionStorage 按 origin + 标签页存活，标记仍在。
 *
 * 单进程内 sessionStorage 是同一个 Map，直接调用测不出跨文档；必须用 vi.resetModules()
 * 重新 import 模块（模拟新文档的新模块实例）+ 换一个 window 对象（模拟新 window）
 * + 复用同一个 storage 对象（模拟同一标签页的 sessionStorage），三者缺一不可。
 *
 * 运行：pnpm --filter @jonex/shell test（shell 的 vitest 收 ../shared/shell-sdk/src/**\/*.test.ts）
 */

const NOW = new Date('2026-09-21T10:00:00.000Z');
const SETTLE_KEY = '__JONEX_LOGOUT_SETTLE_AT__';
const NAV_KEY = '__JONEX_LOGOUT_NAV_AT__';

interface FakeStorage {
  getItem(k: string): string | null;
  setItem(k: string, v: unknown): void;
  removeItem(k: string): void;
  __raw: Map<string, string>;
}

function createMemoryStorage(): FakeStorage {
  const map = new Map<string, string>();
  return {
    getItem: (k) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k, v) => {
      map.set(k, String(v));
    },
    removeItem: (k) => {
      map.delete(k);
    },
    __raw: map,
  };
}

/** 装一个假 window（新文档 = 新对象），sessionStorage 可注入同一份以模拟跨文档 */
function installWindow(storage: FakeStorage | null): void {
  vi.stubGlobal('window', storage ? { sessionStorage: storage } : {});
}

/** 取一份「新文档」下的模块实例：重置模块缓存后重新 import */
async function freshModule() {
  vi.resetModules();
  return import('./logoutNavigation');
}

describe('logoutNavigation', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  describe('常量契约', () => {
    it('键名与窗口期不变（57 线上 bundle 审计以这两个字面量为 grep 锚点）', async () => {
      const mod = await import('./logoutNavigation');
      expect(mod.LOGOUT_NAV_GRACE_MS).toBe(8000);
      expect(mod.LOGOUT_SETTLE_MS).toBe(30000);
    });
  });

  describe('consumeLogoutSettle：基本语义', () => {
    it('mark 之后消费返回 true', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(mod.consumeLogoutSettle()).toBe(true);
    });

    it('一次性：第二次消费返回 false（读后即删）', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(mod.consumeLogoutSettle()).toBe(true);
      expect(mod.consumeLogoutSettle()).toBe(false);
      expect(storage.__raw.has(SETTLE_KEY)).toBe(false);
    });

    it('从未 mark 过 → false（真·会话过期路径必须拿到 false 才会拼 ?expired=1）', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      expect(mod.consumeLogoutSettle()).toBe(false);
    });
  });

  describe('consumeLogoutSettle：自过期与时钟', () => {
    it('超过 LOGOUT_SETTLE_MS 的陈旧标记 → false', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      storage.setItem(SETTLE_KEY, String(NOW.getTime() - (30_000 + 1)));
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('恰好等于 LOGOUT_SETTLE_MS → false（严格小于）', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      storage.setItem(SETTLE_KEY, String(NOW.getTime() - 30_000));
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('差 1ms 未过期 → true', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      storage.setItem(SETTLE_KEY, String(NOW.getTime() - (30_000 - 1)));
      expect(mod.consumeLogoutSettle()).toBe(true);
    });

    it('时钟向后跳（未来时间戳）→ false（下界防护，负数恒小于窗口）', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      storage.setItem(SETTLE_KEY, String(NOW.getTime() + 60_000));
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('空串 / 非数字 / NaN 一律归无效 → false', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      for (const bad of ['', 'abc', 'NaN', 'Infinity']) {
        storage.setItem(SETTLE_KEY, bad);
        expect(mod.consumeLogoutSettle()).toBe(false);
      }
    });
  });

  describe('⭐ 跨文档存活（本次修复的核心）', () => {
    it('登出 → Keycloak 转回新文档（新 window、同一 sessionStorage）仍能消费到 SETTLE', async () => {
      const storage = createMemoryStorage();

      // 文档 1：用户点登出，标记落在当前文档的 sessionStorage
      installWindow(storage);
      const doc1 = await import('./logoutNavigation');
      doc1.markLogoutNavigationInFlight();
      expect(storage.__raw.has(SETTLE_KEY)).toBe(true);

      // 文档 2：KC 302 转回 = 全新文档（新 window 对象、新模块实例），
      // 但同一标签页同一 origin ⇒ sessionStorage 是同一份
      installWindow(storage);
      const doc2 = await freshModule();

      expect(doc2.consumeLogoutSettle()).toBe(true);
    });

    it('反证：若标记只活在 window 上（旧实现），新文档读不到 → 本用例会失败', async () => {
      const storage = createMemoryStorage();

      installWindow(storage);
      const doc1 = await import('./logoutNavigation');
      doc1.markLogoutNavigationInFlight();

      // 新 window 且**不带**同一 storage —— 模拟「标记随 window 丢失」的旧行为
      installWindow(createMemoryStorage());
      const doc2 = await freshModule();

      expect(doc2.consumeLogoutSettle()).toBe(false);
    });
  });

  describe('consumeLogoutSettle：一次性护栏（不依赖 removeItem 成功）', () => {
    it('removeItem 抛异常时，第二次消费仍返回 false', async () => {
      const storage = createMemoryStorage();
      const throwing: FakeStorage = {
        ...storage,
        removeItem: () => {
          throw new Error('storage disabled');
        },
      };
      installWindow(throwing);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(mod.consumeLogoutSettle()).toBe(true);
      // 标记没被删掉（removeItem 抛了），但内存护栏必须挡住第二次
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('重新 mark 后消费资格复位（失败重试登出仍应识别）', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(mod.consumeLogoutSettle()).toBe(true);

      mod.markLogoutNavigationInFlight();
      expect(mod.consumeLogoutSettle()).toBe(true);
    });
  });

  describe('降级：无 window / storage 不可用', () => {
    it('无 window（node 环境）→ mark 不抛、consume 返回 false', async () => {
      vi.stubGlobal('window', undefined);
      const mod = await import('./logoutNavigation');

      expect(() => mod.markLogoutNavigationInFlight()).not.toThrow();
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('window 存在但无 sessionStorage → 静默降级，不抛', async () => {
      installWindow(null);
      const mod = await import('./logoutNavigation');

      expect(() => mod.markLogoutNavigationInFlight()).not.toThrow();
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('读 sessionStorage 属性本身抛 SecurityError（隐私模式）→ 静默降级', async () => {
      vi.stubGlobal('window', {
        get sessionStorage(): never {
          throw new Error('SecurityError');
        },
      });
      const mod = await import('./logoutNavigation');

      expect(() => mod.markLogoutNavigationInFlight()).not.toThrow();
      expect(mod.consumeLogoutSettle()).toBe(false);
    });

    it('getItem 抛异常 → consume 返回 false 且不外抛', async () => {
      const storage = createMemoryStorage();
      const throwing: FakeStorage = {
        ...storage,
        getItem: () => {
          throw new Error('boom');
        },
      };
      installWindow(throwing);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(() => mod.consumeLogoutSettle()).not.toThrow();
      expect(mod.consumeLogoutSettle()).toBe(false);
    });
  });

  describe('回归保护：NAV_GUARD 仍走 window（同文档跨模块，需随导航销毁）', () => {
    it('mark 后同文档内 isLogoutNavigationInFlight 为 true', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(mod.isLogoutNavigationInFlight()).toBe(true);
    });

    it('超过 LOGOUT_NAV_GRACE_MS 后不再在途（自过期，让轮询兜底）', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      vi.setSystemTime(new Date(NOW.getTime() + 8_000));
      expect(mod.isLogoutNavigationInFlight()).toBe(false);
    });

    it('⭐ 新文档（新 window）里 NAV_GUARD 必须消失 —— 否则新文档收尾跳转会被压制 8 秒', async () => {
      const storage = createMemoryStorage();

      installWindow(storage);
      const doc1 = await import('./logoutNavigation');
      doc1.markLogoutNavigationInFlight();

      installWindow(storage); // 同一 storage，但新 window
      const doc2 = await freshModule();

      expect(doc2.isLogoutNavigationInFlight()).toBe(false);
    });

    it('NAV_GUARD 写在 window 上，不落 sessionStorage', async () => {
      const storage = createMemoryStorage();
      installWindow(storage);
      const mod = await import('./logoutNavigation');

      mod.markLogoutNavigationInFlight();
      expect(storage.__raw.has(NAV_KEY)).toBe(false);
    });
  });
});
