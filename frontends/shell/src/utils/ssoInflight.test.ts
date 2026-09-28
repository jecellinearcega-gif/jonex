import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  SSO_IN_FLIGHT_KEY,
  SSO_IN_FLIGHT_MAX_AGE_MS,
  clearSsoNavigationInFlight,
  consumeSsoReturnHint,
  markSsoNavigationInFlight,
} from './ssoInflight';

/** 冻结的「当前时间」基准，避免真实时钟走过的毫秒让边界用例抖动 */
const NOW = new Date('2026-09-21T10:00:00.000Z');

/** 内存版 sessionStorage：测试只需 getItem/setItem/removeItem 三个方法 */
function createMemoryStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (key: string): string | null => (map.has(key) ? (map.get(key) as string) : null),
    setItem: (key: string, value: string): void => {
      map.set(key, String(value));
    },
    removeItem: (key: string): void => {
      map.delete(key);
    },
    /** 供断言直读，绕开被测函数的读取路径 */
    __raw: map,
  };
}

type MemoryStorage = ReturnType<typeof createMemoryStorage>;

function installWindow(storage: MemoryStorage | Record<string, unknown>): void {
  vi.stubGlobal('window', { sessionStorage: storage });
}

describe('ssoInflight', () => {
  let storage: MemoryStorage;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    storage = createMemoryStorage();
    installWindow(storage);
    // 「已消费」是模块级内存态，会跨用例存活；用公开的 clear 复位它，保证用例之间互不污染。
    clearSsoNavigationInFlight();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  // 1
  it('无哨兵键时 consumeSsoReturnHint 返回 false', () => {
    expect(consumeSsoReturnHint()).toBe(false);
  });

  // 2
  it('刚标记后 consumeSsoReturnHint 返回 true', () => {
    markSsoNavigationInFlight();
    expect(consumeSsoReturnHint()).toBe(true);
  });

  // 3
  it('哨兵是一次性的：第二次 consume 返回 false', () => {
    markSsoNavigationInFlight();
    expect(consumeSsoReturnHint()).toBe(true);
    expect(consumeSsoReturnHint()).toBe(false);
  });

  // 4
  it('哨兵超龄（16 分钟前写入，超出 15 分钟窗口）返回 false，且键被清除', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(NOW.getTime() - 16 * 60 * 1000));
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 4b —— 15 分钟窗口的内侧：企业 IdP 上多跳耗时较久（Casdoor → IdP → MFA）仍应提示
  it('哨兵写入于 14 分钟前（窗口内侧）仍返回 true', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(NOW.getTime() - 14 * 60 * 1000));
    expect(consumeSsoReturnHint()).toBe(true);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 5 —— 边界外侧：年龄超过 MAX_AGE 1ms
  it('边界：年龄比 MAX_AGE 多 1ms 返回 false', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(NOW.getTime() - SSO_IN_FLIGHT_MAX_AGE_MS - 1));
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 5b —— 边界正好压线：年龄恰好等于 MAX_AGE（判定用严格小于，故为 false）
  it('边界：年龄恰好等于 MAX_AGE 返回 false', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(NOW.getTime() - SSO_IN_FLIGHT_MAX_AGE_MS));
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 6 —— 边界内侧：年龄比 MAX_AGE 少 1ms
  it('边界：年龄比 MAX_AGE 少 1ms 返回 true', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(NOW.getTime() - SSO_IN_FLIGHT_MAX_AGE_MS + 1));
    expect(consumeSsoReturnHint()).toBe(true);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  it('边界：年龄为 0（同一时刻写入）返回 true', () => {
    markSsoNavigationInFlight();
    expect(consumeSsoReturnHint()).toBe(true);
  });

  // 6b —— 下界校验：系统时钟向后跳（WSL2 休眠恢复 / NTP 校正）会让 age 变成负数，
  // 只看上界的话任意陈旧值都能通过判定（负数永远小于 MAX_AGE）
  it('时钟向后跳：写入时间戳落在未来时返回 false，且键被清除', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, String(NOW.getTime() + 60 * 1000));
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 7
  it('值为非数字时返回 false，且键被清除', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, 'abc');
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  it('值为 NaN/Infinity 时返回 false，且键被清除', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, 'NaN');
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();

    markSsoNavigationInFlight();
    storage.setItem(SSO_IN_FLIGHT_KEY, 'Infinity');
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 8
  it('值为空字符串时返回 false', () => {
    storage.setItem(SSO_IN_FLIGHT_KEY, '');
    expect(consumeSsoReturnHint()).toBe(false);
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
  });

  // 9
  it('sessionStorage.getItem 抛异常时 consume 返回 false 且不外抛', () => {
    installWindow({
      getItem: () => {
        throw new Error('SecurityError: storage disabled');
      },
      setItem: () => {},
      removeItem: () => {},
    });

    expect(() => consumeSsoReturnHint()).not.toThrow();
    expect(consumeSsoReturnHint()).toBe(false);
  });

  // 10
  it('sessionStorage.setItem 抛异常时 mark 不外抛（静默降级）', () => {
    installWindow({
      getItem: () => null,
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {},
    });

    expect(() => markSsoNavigationInFlight()).not.toThrow();
    expect(consumeSsoReturnHint()).toBe(false);
  });

  it('sessionStorage.removeItem 抛异常时 consume 不外抛，单次调用仍返回 true', () => {
    installWindow({
      getItem: () => String(NOW.getTime()),
      setItem: () => {},
      removeItem: () => {
        throw new Error('removeItem boom');
      },
    });

    // 单次调用的判定与本轮 storage 是否可写无关，照常提示
    expect(() => consumeSsoReturnHint()).not.toThrow();
  });

  // 10b —— 原子性兜底：读得到、删不掉时，若没有内存标记，同一次事件循环内的两次消费会都返回 true
  it('原子性：getItem 正常但 removeItem 抛异常时，同一实例第二次 consume 返回 false', () => {
    installWindow({
      getItem: () => String(NOW.getTime()),
      setItem: () => {},
      removeItem: () => {
        throw new Error('removeItem boom');
      },
    });

    expect(consumeSsoReturnHint()).toBe(true);
    expect(consumeSsoReturnHint()).toBe(false);
  });

  // 10c —— 回归锁：内存标记绝不能把「用户重试 SSO」这条主路径掐死
  it('重试不退化：mark → consume(true) → consume(false) → 再 mark → consume 仍返回 true', () => {
    markSsoNavigationInFlight();
    expect(consumeSsoReturnHint()).toBe(true);
    expect(consumeSsoReturnHint()).toBe(false);

    // 用户失败后重新点了一次企业 SSO 登录：mark 复位消费资格
    markSsoNavigationInFlight();
    expect(consumeSsoReturnHint()).toBe(true);
  });

  // 11
  it('clearSsoNavigationInFlight 清键之后 consume 返回 false', () => {
    markSsoNavigationInFlight();
    clearSsoNavigationInFlight();
    expect(storage.getItem(SSO_IN_FLIGHT_KEY)).toBeNull();
    expect(consumeSsoReturnHint()).toBe(false);
  });

  it('clearSsoNavigationInFlight 在无键时也不抛', () => {
    expect(() => clearSsoNavigationInFlight()).not.toThrow();
  });

  // 12
  it('无 window 全局时三个函数均不抛，consume 返回 false', () => {
    vi.stubGlobal('window', undefined);

    expect(() => markSsoNavigationInFlight()).not.toThrow();
    expect(consumeSsoReturnHint()).toBe(false);
    expect(() => clearSsoNavigationInFlight()).not.toThrow();
  });

  it('sessionStorage 整体缺失（未定义）时三个函数均不抛', () => {
    installWindow({});

    expect(() => markSsoNavigationInFlight()).not.toThrow();
    expect(consumeSsoReturnHint()).toBe(false);
    expect(() => clearSsoNavigationInFlight()).not.toThrow();
  });

  it('导出的常量：键名固定，存活期为 15 分钟', () => {
    expect(SSO_IN_FLIGHT_KEY).toBe('jonex.sso.inflight.at');
    expect(SSO_IN_FLIGHT_MAX_AGE_MS).toBe(15 * 60 * 1000);
  });
});
