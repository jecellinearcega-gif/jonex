/**
 * [jonex] 列式视图的抽取核心（纯函数，可单测）。
 *
 * 「列式图谱」要回答三类问题，本文件只负责**最有信息量的一类**：把散落在
 * 编译产物里的「日期 × 品类」对齐成一张矩阵。
 *
 * 真实产物里有两处可用来源（都不是猜的）：
 * 1) **表格页的 sheet 样例**（首选）：引擎把工作表片段以管道行写进页面正文，
 *    表头是星期名（`星期一`）或 Excel 序列号（`45747`），行首是「餐次 + 档口」
 *    两级标签，单元格是具体菜品。这类结构可靠、可逐格核对。
 * 2) **日期页正文**（兜底）：`4月28日早餐中式档口供应肉包、紫米馒头、南瓜糕。`
 *    按「日期 + 标签 + 供应/为 + 顿号列表」切分；拿不准的句子进「其他」行，不猜。
 *
 * 日期对齐优先用引擎自己算好的 **日期 → 星期 → 列** 映射（workbook 页里的
 * markdown 表），其次用表头里的序列号直接换算，最后才用星期名。
 * 一律输出 ISO 日期字符串，列按日期排序——跨工作表、跨页面都能合并成一张表。
 */

/** 表格页/日期页的统一输入（正文 markdown + 所在工作表名） */
export interface ColumnarDoc {
  path: string;
  title: string;
  markdown: string;
  /** 引擎 scope.sheet（表格页才有），用于把星期名对到具体日期 */
  sheet?: string;
}

/** 一行 = 一个品类标签；values 是「日期 → 单元格内容」 */
export interface TimelineRow {
  label: string;
  values: Record<string, string>;
}

export interface TimelineMatrix {
  columns: string[];
  rows: TimelineRow[];
  /** sheet = 来自表格样例（可靠）；prose = 来自日期页正文（启发式）；none = 无数据 */
  origin: 'sheet' | 'prose' | 'none';
  sheets: string[];
}

/** 引擎给的「日期 → 星期 → 列」映射中的一条 */
export interface DateMapEntry {
  sheet: string;
  date: string;
  weekday: string;
  column: string;
  raw: string;
}

const WEEKDAYS = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日'];
const WEEKDAY_ALIASES: Record<string, string> = {
  周一: '星期一',
  周二: '星期二',
  周三: '星期三',
  周四: '星期四',
  周五: '星期五',
  周六: '星期六',
  周日: '星期日',
  周天: '星期日',
  Monday: '星期一',
  Tuesday: '星期二',
  Wednesday: '星期三',
  Thursday: '星期四',
  Friday: '星期五',
  Saturday: '星期六',
  Sunday: '星期日',
};

const SERIAL_MIN = 20000;
const SERIAL_MAX = 80000;

/** Excel 序列号（1900 日期系统）→ ISO 日期 */
export function excelSerialToIso(serial: number): string | null {
  if (!Number.isFinite(serial) || serial < SERIAL_MIN || serial > SERIAL_MAX) return null;
  // 1899-12-30 是 Excel 序列号 0（含 1900 闰年 bug 的既定口径）
  const ms = Date.UTC(1899, 11, 30) + Math.round(serial) * 86400000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 10);
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

/** 把 `2025-4-28` / `2025年4月28日` / `4月28日`(+year) 统一成 ISO 日期 */
export function normalizeDateText(text: string, defaultYear?: number): string | null {
  const raw = (text || '').trim();
  if (!raw) return null;
  let m = /^(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})/.exec(raw);
  if (m) return `${m[1]}-${pad(Number(m[2]))}-${pad(Number(m[3]))}`;
  m = /^(\d{1,2})[-/月.](\d{1,2})日?/.exec(raw);
  if (m && defaultYear) return `${defaultYear}-${pad(Number(m[1]))}-${pad(Number(m[2]))}`;
  return null;
}

function normalizeWeekday(text: string): string | null {
  const raw = (text || '').trim();
  if (!raw) return null;
  if (WEEKDAYS.includes(raw)) return raw;
  return WEEKDAY_ALIASES[raw] || null;
}

/** 单元格 → 日期（ISO）：直接日期 > Excel 序列号 > 星期名（需 dateMap 上下文） */
function cellToDate(cell: string, ctx: ResolveContext): string | null {
  const raw = (cell || '').trim();
  if (!raw) return null;
  const direct = normalizeDateText(raw, ctx.defaultYear);
  if (direct) return direct;
  if (/^\d{5}$/.test(raw)) {
    const iso = excelSerialToIso(Number(raw));
    if (iso) return iso;
  }
  const weekday = normalizeWeekday(raw);
  if (weekday) {
    const key = ctx.sheet ? `${ctx.sheet}|${weekday}` : `|${weekday}`;
    const hit = ctx.dateMap.get(key);
    if (hit) return hit;
    // 跨工作表兜底：星期名唯一命中时才用（同一星期在多张表里会指向不同日期）
    const candidates = new Set<string>();
    ctx.dateMap.forEach((date, mapKey) => {
      if (mapKey.endsWith(`|${weekday}`)) candidates.add(date);
    });
    if (candidates.size === 1) return [...candidates][0];
  }
  return null;
}

interface ResolveContext {
  dateMap: Map<string, string>;
  sheet?: string;
  defaultYear?: number;
}

/**
 * 把正文里的管道行解析成单元格矩阵。
 *
 * 引擎写表格样例的格式是 `c1 | c2 | … | cn`（**无**首尾竖线），例如：
 *   ` |  | 星期一 | 总数量 | … | 剩余数量`（表头，首列空）
 *   `早 | 中式 | 肉包 |  |  | 香菇肉包 | … |  `（数据行）
 * 因此按下标硬切、**不做首尾裁剪**，各行下标才能对齐（实测产物每行都是 14 段）。
 * 标准 markdown 表（`| a | b |`）会多出首尾两个空段，对矩阵无影响。
 */
export function parsePipeRows(markdown: string): string[][] {
  const rows: string[][] = [];
  for (const line of (markdown || '').replace(/\r\n/g, '\n').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed.includes('|')) continue;
    const cells = trimmed.split('|').map((c) => c.trim());
    if (cells.length < 2) continue;
    // markdown 分隔行（|---|---|）不是数据
    if (cells.every((c) => /^:?-{2,}:?$/.test(c) || c === '')) continue;
    rows.push(cells);
  }
  return rows;
}

/**
 * 解析引擎给的「日期 → 星期 → 列」映射表（workbook 页正文里的 markdown 表）。
 *
 * 表头形如 `| 工作表 | 日期 | 星期 | 列 | 表头原值 |`；表头原值可能是星期名，
 * 也可能是 Excel 序列号——这正是表格页表头对不上日期时需要的那把钥匙。
 */
export function parseDateMap(markdown: string): DateMapEntry[] {
  const lines = (markdown || '').replace(/\r\n/g, '\n').split('\n');
  const entries: DateMapEntry[] = [];
  let header: string[] | null = null;
  const flush = () => {
    header = null;
  };
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('|')) {
      flush();
      continue;
    }
    const cells = trimmed.split('|').map((c) => c.trim());
    if (cells.length && cells[0] === '') cells.shift();
    if (cells.length && cells[cells.length - 1] === '') cells.pop();
    if (cells.every((c) => /^:?-{2,}:?$/.test(c) || c === '')) continue;
    if (!header) {
      const hasDate = cells.some((c) => /日期|date/i.test(c));
      const hasSheet = cells.some((c) => /工作表|sheet/i.test(c));
      if (hasDate && hasSheet) {
        header = cells.map((c) => c.toLowerCase());
        continue;
      }
      continue;
    }
    const get = (pattern: RegExp): string => {
      const idx = header!.findIndex((h) => pattern.test(h));
      return idx >= 0 ? cells[idx] || '' : '';
    };
    const date = normalizeDateText(get(/日期|date/), undefined);
    const sheet = get(/工作表|sheet/);
    if (!date || !sheet) continue;
    entries.push({
      sheet,
      date,
      weekday: get(/星期|weekday|周/) || '',
      column: get(/^列$|column/),
      raw: get(/表头原值|raw|原值/),
    });
  }
  return entries;
}

function buildDateMap(entries: DateMapEntry[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const e of entries) {
    if (e.raw) map.set(`${e.sheet}|${e.raw}`, e.date);
    if (e.weekday) map.set(`${e.sheet}|${e.weekday}`, e.date);
    if (e.column) map.set(`${e.sheet}|${e.column}`, e.date);
  }
  return map;
}

/** 从所有页面里收集「日期 → 星期 → 列」映射（通常在 workbook 骨架页里） */
export function collectDateMap(docs: ColumnarDoc[]): DateMapEntry[] {
  const out: DateMapEntry[] = [];
  for (const doc of docs) out.push(...parseDateMap(doc.markdown));
  return out;
}

function defaultYearFrom(docs: ColumnarDoc[], entries: DateMapEntry[]): number | undefined {
  const years = new Map<string, number>();
  const bump = (text: string) => {
    const m = /(\d{4})[-/年.]\d{1,2}/.exec(text || '');
    if (m) years.set(m[1], (years.get(m[1]) || 0) + 1);
  };
  entries.forEach((e) => bump(e.date));
  docs.forEach((d) => {
    bump(d.path);
    bump(d.title);
  });
  let best: string | undefined;
  years.forEach((count, year) => {
    if (!best || count > (years.get(best) || 0)) best = year;
  });
  return best ? Number(best) : undefined;
}

/** 表格样例 → 矩阵行（含跨表合并） */
export function extractSheetMatrix(
  docs: ColumnarDoc[],
  dateMap: Map<string, string>,
  defaultYear?: number,
): { rows: TimelineRow[]; columns: string[]; sheets: string[] } {
  const rowMap = new Map<string, Record<string, string>>();
  const columns = new Set<string>();
  const sheets = new Set<string>();

  for (const doc of docs) {
    const ctx: ResolveContext = { dateMap, sheet: doc.sheet, defaultYear };
    const pipeRows = parsePipeRows(doc.markdown);
    if (pipeRows.length < 2) continue;
    let dateByColumn = new Map<number, string>();
    // 行首标签的前向填充（合并单元格在样例里就是空串）
    let labelParts: string[] = [];
    let used = false;

    for (const cells of pipeRows) {
      const resolvable = cells
        .map((cell, index) => [index, cellToDate(cell, ctx)] as const)
        .filter(([, date]) => !!date) as [number, string][];
      // 表头行判定：至少 2 个**结构性**日期单元格（星期名或 Excel 序列号）。
      // 直接写 ISO 日期的行是「日期 → 星期」映射表本身，不是表头，必须排除，
      // 否则映射表会被误判成表头，把整张矩阵的列对齐搞乱。
      const structural = resolvable.filter(([index]) => !normalizeDateText(cells[index] || '', ctx.defaultYear));
      if (structural.length >= 2) {
        dateByColumn = new Map(resolvable);
        labelParts = [];
        continue;
      }
      if (dateByColumn.size === 0) continue;
      const firstData = Math.min(...dateByColumn.keys());
      const rawLabels = cells.slice(0, firstData);
      labelParts = rawLabels.map((cell, i) => (cell ? cell : labelParts[i] || ''));
      const label = labelParts.filter(Boolean).join('·').trim();
      if (!label) continue;
      let wrote = false;
      const values: Record<string, string> = rowMap.get(label) || {};
      dateByColumn.forEach((date, index) => {
        const value = (cells[index] || '').trim();
        if (!value) return;
        // 同一格多次出现时保留首个（多张表合并时以先出现的为准，避免抖动）
        if (!values[date]) {
          values[date] = value;
          columns.add(date);
          wrote = true;
        }
      });
      if (wrote) {
        rowMap.set(label, values);
        used = true;
        if (doc.sheet) sheets.add(doc.sheet);
      }
    }
    if (!used && doc.sheet) {
      // 该页没有可用矩阵数据 → 不计入工作表清单
    }
  }

  const rows: TimelineRow[] = [...rowMap.entries()].map(([label, values]) => ({ label, values }));
  return { rows, columns: [...columns].sort(), sheets: [...sheets] };
}

/**
 * 日期页正文 → 矩阵行（兜底路径）。
 *
 * 真实句式（菜单语料）：
 *   `4月28日早餐中式档口供应肉包、紫米馒头、南瓜糕。`
 *   `4月28日早餐杂粮为红薯，粥品为燕麦粥，蔬菜为京包菜。`
 * 切分规则：先按句号断句，再按逗号切子句；每条子句取「标签 + 供应/为/： + 值」。
 * 抽不出标签的整句进「其他」，不猜。
 */
export function extractProseMatrix(
  docs: ColumnarDoc[],
  defaultYear?: number,
): {
  rows: TimelineRow[];
  columns: string[];
} {
  const rowMap = new Map<string, Record<string, string>>();
  const columns = new Set<string>();
  const others: Record<string, string> = {};
  let hasOthers = false;

  for (const doc of docs) {
    const docYear = (() => {
      const m = /(\d{4})[-/年.]\d{1,2}/.exec(`${doc.path} ${doc.title}`);
      return m ? Number(m[1]) : defaultYear;
    })();
    const text = (doc.markdown || '')
      .replace(/^---[\s\S]*?---/m, '')
      .replace(/^#{1,6}.*$/gm, '')
      .replace(/```[\s\S]*?```/g, '');
    for (const rawLine of text.split(/\n|。/)) {
      const line = rawLine.trim();
      if (!line) continue;
      const dateHit = /(\d{4}年)?\d{1,2}月\d{1,2}日?/.exec(line);
      if (!dateHit) continue;
      const date = normalizeDateText(dateHit[0], docYear);
      if (!date) continue;
      const clauses = line.split(/[，,；;]/);
      for (const clause of clauses) {
        const m = /^(.{0,24}?)(?:供应|有|为|：|:)(.+)$/.exec(clause.trim());
        if (!m) {
          if (!others[date] && clause.trim()) {
            others[date] = clause.trim().slice(0, 80);
            hasOthers = true;
          }
          continue;
        }
        // 标签：去掉日期与「的」，保留「早餐/午餐/晚餐 + 档口/品类」
        let label = m[1].replace(dateHit[0], '').replace(/^的/, '').trim();
        label = label.replace(/\s+/g, '');
        const value = m[2].replace(/[。；;]$/, '').trim();
        if (!label || !value) continue;
        const values = rowMap.get(label) || {};
        if (!values[date]) {
          values[date] = value;
          rowMap.set(label, values);
          columns.add(date);
        }
      }
    }
  }

  const rows: TimelineRow[] = [...rowMap.entries()].map(([label, values]) => ({ label, values }));
  if (hasOthers) rows.push({ label: '__other__', values: others });
  return { rows, columns: [...columns].sort() };
}

/** 编排：先试表格样例（可靠），再退到日期页正文，都没有就返回 none */
export function buildTimelineMatrix(docs: ColumnarDoc[]): TimelineMatrix {
  const dateEntries = collectDateMap(docs);
  const dateMap = buildDateMap(dateEntries);
  const defaultYear = defaultYearFrom(docs, dateEntries);

  const sheetDocs = docs.filter((d) => !!d.sheet && parsePipeRows(d.markdown).length >= 2);
  const sheetResult = extractSheetMatrix(sheetDocs.length ? sheetDocs : docs, dateMap, defaultYear);
  if (sheetResult.rows.length > 0 && sheetResult.columns.length > 0) {
    return { ...sheetResult, origin: 'sheet' };
  }

  const prose = extractProseMatrix(docs, defaultYear);
  if (prose.rows.length > 0 && prose.columns.length > 0) {
    return { columns: prose.columns, rows: prose.rows, origin: 'prose', sheets: [] };
  }
  return { columns: [], rows: [], origin: 'none', sheets: [] };
}

/** 矩阵 → 表格数据（前端渲染用：行标签 + 每列单元格） */
export function matrixToGrid(matrix: TimelineMatrix): {
  header: string[];
  body: { label: string; cells: string[] }[];
} {
  return {
    header: matrix.columns,
    body: matrix.rows.map((row) => ({
      label: row.label,
      cells: matrix.columns.map((date) => row.values[date] || ''),
    })),
  };
}
