import type { SubtaskStatus, TaskStatus } from '../../api/taskCenter';
import type { TaskSource, TaskType } from './types';

/**
 * 状态映射表。任务与子任务是**两套独立取值**，不能互相替代：
 * 成功在任务上是 completed、在子任务上是 success，子任务还多一个 timeout。
 */

export const TASK_STATUS_LABEL_KEY: Record<TaskStatus, string> = {
  pending: 'taskCenter.status.pending',
  running: 'taskCenter.status.running',
  completed: 'taskCenter.status.completed',
  failed: 'taskCenter.status.failed',
};

/** 状态标签栏的顺序（不含「全部」——它由页面补在最前）。 */
export const TASK_STATUS_ORDER: readonly TaskStatus[] = ['pending', 'running', 'completed', 'failed'];

/**
 * 状态 Tag 的颜色，按 antd 状态色语义取值：进行中用 processing（蓝）而不是 success（绿），
 * 绿色读作「已结束且成功」。
 */
export const TASK_STATUS_COLORS: Record<TaskStatus, string> = {
  pending: 'default',
  running: 'processing',
  completed: 'success',
  failed: 'error',
};

export const SUBTASK_STATUS_LABEL_KEY: Record<SubtaskStatus, string> = {
  pending: 'taskCenter.subtaskStatus.pending',
  running: 'taskCenter.subtaskStatus.running',
  success: 'taskCenter.subtaskStatus.success',
  failed: 'taskCenter.subtaskStatus.failed',
  timeout: 'taskCenter.subtaskStatus.timeout',
};

export const SUBTASK_STATUS_COLORS: Record<SubtaskStatus, string> = {
  pending: 'default',
  running: 'processing',
  success: 'success',
  failed: 'error',
  timeout: 'warning',
};

/**
 * 子任务流水线（Timeline）节点的颜色。与上面那张**不是**同一套取值，不能互相替代：
 * Timeline 只认 blue/red/green/gray 四个预设字面量，其余值会被当成 CSS 颜色塞进变量而静默失效。
 */
export const SUBTASK_STATUS_DOT_COLORS: Record<SubtaskStatus, string> = {
  pending: 'gray',
  running: 'blue',
  success: 'green',
  failed: 'red',
  // Timeline 没有「超时」这个色档，归到红：它与失败同样是需要人看的结局
  timeout: 'red',
};

/**
 * ── 筛选项枚举 ──
 *
 * 与上面几张状态表的区别：状态是**接口字段**的直接映射（键类型来自 `api/taskCenter`），
 * 这两张是**筛选参数**的取值，接口目前还不接收（见 `types.ts` 的 `TaskFilters`）。
 *
 * 下拉顺序即对象键的书写顺序（`Object.entries` 对字符串键保持插入序），改顺序就改这里。
 */

/** 任务来源。取值与后端 `ACCESS_TYPES` 逐字一致。 */
export const TASK_SOURCE_LABEL_KEY: Record<TaskSource, string> = {
  file: 'taskCenter.source.file',
  api: 'taskCenter.source.api',
  api_push: 'taskCenter.source.apiPush',
  storage: 'taskCenter.source.storage',
};

/** 任务类型。前三个是单个阶段（复用阶段名文案），full 是三阶段全跑。 */
export const TASK_TYPE_LABEL_KEY: Record<TaskType, string> = {
  parse: 'taskCenter.stage.parse',
  ontology: 'taskCenter.stage.ontology',
  wiki: 'taskCenter.stage.wiki',
  full: 'taskCenter.type.full',
};

/**
 * ── 写操作的门禁（与后端前置逐字对齐）──
 *
 * 放在这里而不是页面里：子任务的两个动作有**两处渲染点**（展开行的 Timeline、抽屉的
 * Collapse），判定散开迟早会各判一套。
 */

/** 任务已终态。后端 `_require_terminal` 认的就是这两个，重跑的前置。 */
export function isTaskTerminal(status: TaskStatus): boolean {
  return status === 'completed' || status === 'failed';
}

/**
 * 某一步现在能用哪个动作，`null` = 什么都不给。
 *
 * - 任务**没跑完**：只有还没开始的（`pending`）能停 —— `running` 的停不掉（运行方没有
 *   停止动作，后端拒 1003）。
 * - 任务**已终态**：跑完了的（`success` / `failed` / `timeout`）能重跑 —— 没跑完的自然
 *   无从谈起。
 *
 * ⚠️ 「上游是否全 `success`」**判不了**：控制台 DTO 不含 `depends_on`，这一档交给后端
 * 回 1001、前端原样展示它的 `message`（后端已按 locale 译好）。
 */
export function subtaskAction(taskStatus: TaskStatus, subtaskStatus: SubtaskStatus): 'stop' | 'retry' | null {
  if (!isTaskTerminal(taskStatus)) {
    return subtaskStatus === 'pending' ? 'stop' : null;
  }
  return subtaskStatus === 'success' || subtaskStatus === 'failed' || subtaskStatus === 'timeout' ? 'retry' : null;
}
