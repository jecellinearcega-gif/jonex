import type { Dayjs } from 'dayjs';

/**
 * 任务中心的页面层类型。
 *
 * 接口 DTO 不在这里 —— 它们在 `src/api/taskCenter.ts`，与请求函数共存（同 auditLogs.ts
 * 等模块的惯例）。本文件只剩页面自己的东西：筛选值。
 */

/**
 * 筛选值。key 与 CommonTable 的 filters[].key 一一对应，也是 fetch 参数的字段名。
 *
 * 必须是 type 别名而非 interface：CommonTable 的 `F extends Record<string, unknown>`
 * 要求隐式索引签名，TS 只给类型别名补、不给 interface 补（TS #15300）。
 */
/**
 * 任务来源。取值**照后端取**：与知识库侧 `dtos/data_source.py` 的 `ACCESS_TYPES` 逐字一致
 * （后端那集合就是这四个：api / api_push / storage / file）。
 */
export type TaskSource = 'file' | 'api' | 'api_push' | 'storage';

/**
 * 任务类型。⚠️ **页面层的聚合视图，后端没有这个字段** —— 后端靠 `definition_id` 区分
 * 一份定义跑几步（三步 Ontology / 两步 Wiki），不存在「只跑某个阶段」这个类型维度。
 * 取值与文案是接接口前的遗留，保留是为对齐将来的筛选参数，语义待后端补齐时确认。
 */
export type TaskType = 'parse' | 'ontology' | 'wiki' | 'full';

export type TaskFilters = {
  keyword?: string;
  /**
   * 任务类型 / 任务来源。⚠️ 这两个参数**后端 `page_tasks` 目前不接收** —— 它只取
   * keyword / start_time / end_time / statuses / page / page_size，多传的键被
   * `d.get(...)` 逐个取值那层静默忽略（不报错、也不生效）。
   *
   * 照发是既定决定：前端一次接好，后端补齐参数后自动生效。参数名按后端 snake_case
   * 风格取，与 start_time / page_size 一致。
   */
  task_type?: string;
  source?: string;
  /**
   * 日期区间 [起始, 结束]；fetch 里拆成 start_time / end_time。
   *
   * 存 dayjs 对象而不是 ISO 串：RangePicker 的 value 要 dayjs，存串就得在 value 上反解析一次。
   * 与同子应用 OperationLog 的做法一致。
   */
  range?: [Dayjs, Dayjs] | null;
  /** 由状态标签栏经 filterExtraRow 写入 —— 它不在 filters 数组里，但同属这份筛选值。 */
  status?: string;
};
