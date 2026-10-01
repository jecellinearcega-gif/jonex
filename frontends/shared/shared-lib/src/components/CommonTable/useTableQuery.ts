import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FilterContext, TableQueryParams } from './types';

/** 空筛选值不参与请求，避免向后端发 keyword= / phase= 这类空参数。 */
function isEmptyFilterValue(value: unknown): boolean {
  if (value === undefined || value === null || value === '') return true;
  return Array.isArray(value) && value.length === 0;
}

/**
 * 合并分页字段与筛选值。paginated 为 false 时 page 恒为 1 —— 不分页模式下组件
 * 只在筛选变化时取数，页码无意义，写死 1 是为了让 fetch 签名对页面保持单一形状。
 */
export function buildQueryParams<F extends Record<string, unknown>>(
  page: number,
  pageSize: number,
  filters: Partial<F>,
  paginated: boolean,
): TableQueryParams<F> {
  const params: Record<string, unknown> = { page: paginated ? page : 1, pageSize };
  for (const [key, value] of Object.entries(filters)) {
    if (!isEmptyFilterValue(value)) params[key] = value;
  }
  return params as TableQueryParams<F>;
}

export interface UseTableQueryOptions<Row, F extends Record<string, unknown>> {
  fetch: (params: TableQueryParams<F>) => Promise<{ list: Row[]; total: number }>;
  paginated: boolean;
  defaultPageSize: number;
  onError?: (e: unknown) => void;
  onQueryChange?: (params: TableQueryParams<F>) => void;
}

export interface UseTableQueryResult<Row, F extends Record<string, unknown>> {
  list: Row[];
  total: number;
  loading: boolean;
  page: number;
  pageSize: number;
  filters: Partial<F>;
  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  setFilter: <K extends keyof F>(key: K, value: F[K]) => void;
  refresh: (options?: { resetPage?: boolean }) => void;
  reset: () => void;
  filterContext: FilterContext<F>;
}

export function useTableQuery<Row, F extends Record<string, unknown>>({
  fetch,
  paginated,
  defaultPageSize,
  onError,
  onQueryChange,
}: UseTableQueryOptions<Row, F>): UseTableQueryResult<Row, F> {
  const [list, setList] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(defaultPageSize);
  const [filters, setFilters] = useState<Partial<F>>({});
  const [tick, setTick] = useState(0);

  // fetch / onError 通常是页面内联的箭头函数，每次渲染都是新引用。把它们放进
  // effect 依赖会让「渲染 → 新引用 → effect 重跑 → setState → 渲染」无限循环，
  // 所以用 ref 持有最新值，effect 只观测查询条件本身。
  const fetchRef = useRef(fetch);
  fetchRef.current = fetch;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const onQueryChangeRef = useRef(onQueryChange);
  onQueryChangeRef.current = onQueryChange;

  // 快慢请求竞态：慢的旧请求可能后于新请求返回，只接受最后一次发出的结果。
  const seqRef = useRef(0);

  useEffect(() => {
    const seq = ++seqRef.current;
    setLoading(true);
    fetchRef
      .current(buildQueryParams(page, pageSize, filters, paginated))
      .then((res) => {
        if (seq !== seqRef.current) return;
        setList(res.list);
        setTotal(res.total);
      })
      .catch((err: unknown) => {
        if (seq !== seqRef.current) return;
        // 失败时清空列表：残留的旧数据会被误读成「筛选后的最新结果」。
        // 失败本身不在这里呈现——表格照常渲染成空态，报警由页面在 onError 里自己发
        setList([]);
        setTotal(0);
        onErrorRef.current?.(err);
      })
      .finally(() => {
        if (seq !== seqRef.current) return;
        setLoading(false);
      });
  }, [page, pageSize, filters, paginated, tick]);

  // 与上面的取数 effect 分开，且**不依赖 tick**：tick 是 refresh() 用来重取同一份数据的，
  // 条件没变就不该通知页面——否则点一次「刷新」就把用户已勾选的行清空了。
  useEffect(() => {
    onQueryChangeRef.current?.(buildQueryParams(page, pageSize, filters, paginated));
  }, [page, pageSize, filters, paginated]);

  const setFilter = useCallback(<K extends keyof F>(key: K, value: F[K]) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    // 筛选条件变了，页码必须归 1，否则会停在一个按新条件可能不存在的页上
    setPage(1);
  }, []);

  const refresh = useCallback((options?: { resetPage?: boolean }) => {
    if (options?.resetPage) setPage(1);
    // 无论页码是否真的变过都要 +1：页面停在第 1 页时 setPage(1) 是空操作，
    // 只靠它取数 effect 不会重跑，切换目录这类请求就会丢。
    setTick((t) => t + 1);
  }, []);

  // 整体清空，filterExtra 写进来的 key 一并清掉（它与配置项共用这一份 state）。
  // 每次都换新对象，所以即使条件本来就是空的，点重置也会重新取一次数。
  const reset = useCallback(() => {
    setFilters({});
    setPage(1);
  }, []);

  const filterContext = useMemo<FilterContext<F>>(
    () => ({
      values: filters,
      get: (key) => filters[key],
      set: setFilter,
    }),
    [filters, setFilter],
  );

  return {
    list,
    total,
    loading,
    page,
    pageSize,
    filters,
    setPage,
    setPageSize,
    setFilter,
    refresh,
    reset,
    filterContext,
  };
}
