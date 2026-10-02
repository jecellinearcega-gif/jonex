import apiClient from './request';

/**
 * 任务中心的六个接口（设计文档 §4.4，后端唯一开给前端的口子）：两个只读 + 四个写。
 *
 * 字段口径的权威定义在 `capabilities/task_center/dtos/console.py`，此处逐字对齐。
 * **没有删除 / 详情接口** —— 再加之前先回设计文档确认边界。
 */

/** 任务状态。与子任务状态**不是同一套**取值：成功在这里叫 completed。 */
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed';

/** 子任务状态。成功叫 success，且多一个 timeout。 */
export type SubtaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'timeout';

export interface TaskConsoleItem {
  id: string;
  /** 发起时没给就为空；后端会退化成编排定义名。 */
  task_name: string | null;
  status: TaskStatus;
  /** ISO 8601。 */
  started_at: string | null;
  finished_at: string | null;
  /** 耗时整秒数；还没跑完就是空。 */
  duration_seconds: number | null;
}

export interface TaskConsolePage {
  items: TaskConsoleItem[];
  page: number;
  page_size: number;
  total: number;
  /** 四个状态键恒在（没有的为 0）。只受「除 status 外」的筛选条件影响。 */
  status_counts: Record<string, number>;
}

export interface SubtaskConsoleItem {
  id: string;
  /**
   * 这条子任务在编排里的节点名（如 `parse_1`）。
   *
   * ⚠️ **不是**同一行的 `id`：`stopSubtask` / `retrySubtask` 只用它寻址，`id` 在页面上
   * 只用于展示与 React key。这两个 id 长得像、含义完全不同，混用会打到别人身上。
   */
  node_id: string;
  /** 内置清单里的步骤名（文件解析 / 知识抽取 / 知识编译），查不到才退化成 step_code。 */
  subtask_name: string | null;
  status: SubtaskStatus;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  /**
   * 产出「去哪儿看」的引用（后端 `List[Dict[str, Any]]`）。**形状由运行方定义**，
   * 管理方只搬运 —— 所以这里不发明字段名，本轮也**不渲染**（`href` 是运行方自己的
   * API 路径，浏览器直接跳会丢 `Authorization` 头，得先定「API 路径 → 子应用页面」
   * 的映射另案处理）。
   */
  output_refs?: Record<string, unknown>[] | null;
}

/**
 * 四个写接口的返回：**内部契约 `TaskItemResponse` 的故意子集**。
 *
 * ⚠️ 它**不是** `TaskConsoleItem` —— 那份是列表 DTO，多一个 `duration_seconds`。
 * 所以拿到它**不要把整个对象摊进页面的 `detail` 状态**（摊了 `duration_seconds` 就没了），
 * 页面只取 `status` 用来更新门禁。完整形状见 `jonex_core/task_protocol/models.py`。
 */
export interface TaskWriteResult {
  id: string;
  status: TaskStatus;
}

export interface TaskListParams {
  /** 模糊匹配任务名或任务 id。 */
  keyword?: string;
  /**
   * 领域空间（取值是**主应用当前选中的空间**，不是页面上的下拉）。
   *
   * ⚠️ 与 `task_type` / `source` 一样，后端 `page_tasks` 目前**不接收** —— 它只取
   * keyword / start_time / end_time / statuses / page / page_size，多传的键被 dispatch
   * 那层逐个取值时静默忽略。照发是既定决定：前端一次接好，后端补齐后自动生效。
   *
   * 注意任务**确实有空间归属**（`retry_subtask` 的 `space_id` 是必传的归属凭据，
   * 见 `_require_space_match`），所以这个参数不是凭空加的维度，只是列表口还没开。
   */
  space_id?: string;
  task_type?: string;
  source?: string;
  /** 按**建任务时间**筛（含两端）。 */
  start_time?: string;
  end_time?: string;
  /** 多选。页面当前是单选，传单元素数组。 */
  status?: TaskStatus[];
  page?: number;
  page_size?: number;
}

export async function listTasks(params: TaskListParams): Promise<TaskConsolePage> {
  // status 是数组：axios 默认序列化成 `status[]=a&status[]=b`，后端认的是可重复参数
  // （`?status=a&status=b`），所以必须显式指定 indexes: null。
  // 已实测：默认 `status%5B%5D=a`，加了这个选项才是 `status=a&status=b`。
  const query: Record<string, unknown> = { ...params };
  if (!params.status?.length) {
    delete query.status;
  }
  return apiClient.get<TaskConsolePage>('/task-center/tasks', {
    params: query,
    paramsSerializer: { indexes: null },
  });
}

/** 子任务列表。**不分页、回裸数组**（设计文档 §4.0 点名的「不设分页」接口）。 */
export async function listSubtasks(taskId: string): Promise<SubtaskConsoleItem[]> {
  return apiClient.get<SubtaskConsoleItem[]>(`/task-center/tasks/${taskId}/subtasks`);
}

/**
 * 停止任务。任务必须是 `pending` / `running`，且底下还要有没开始的子任务。
 *
 * 失败有两种拒绝情形（已终态 / running 但无 pending 子任务），都回同一个冲突码，
 * 调用方不需要自己区分 —— 直接展示后端返回的 `message` 即可。
 */
export async function stopTask(taskId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/stop`);
}

/**
 * 重跑整条任务（原地重来，不新建任务）。
 *
 * 前提是任务**已终态**（`completed` / `failed`），否则后端回 1003 —— 页面对应地用
 * `TASK_STATUS_*` 判终态来收敛按钮，不让用户点了才被拒。
 */
export async function retryTask(taskId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/retry`);
}

/**
 * 停止某一步。`nodeId` 传的是**节点名**（`SubtaskConsoleItem.node_id`，如 `parse_1`），
 * 不是子任务那一行的 `id` —— 后端按 node_id 寻址。
 *
 * 只有 `pending` 的那一步能停（`running` 的停不掉，运行方没有停止动作）。
 * ⚠️ 停完可能把**整条任务**收口成 `failed`，所以返回值里的 `status` 要消费掉。
 */
export async function stopSubtask(taskId: string, nodeId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/subtasks/${encodeURIComponent(nodeId)}/stop`);
}

/**
 * 重跑某一步。`nodeId` 同样是**节点名**，不是行 id。
 *
 * 前提三条：任务已终态、这一步也跑完了、它的上游全部成功 —— 后一条前端判不了
 * （控制台 DTO 不含 `depends_on`），交给后端拒并原样展示 `message`。
 * ⚠️ 重跑会把任务拉回 `running`，兄弟步骤的重试链接要立刻消失（靠调用方写后刷新）。
 */
export async function retrySubtask(taskId: string, nodeId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/subtasks/${encodeURIComponent(nodeId)}/retry`);
}

/**
 * 任务中心的两个权限码（照 `api/tenants.ts` 的写法，常量与接口同处一个文件）。
 *
 * scope 是**租户级**：平台管理员与租户管理员持码，普通用户不持。
 * 它们不只是入口可见性 —— 能力侧 `capabilities/task_center/capability.py` 的
 * `_ACTION_PERMISSIONS` 真的按这两个码判权（read 管两个只读接口，write 管**四个**写
 * action：`task.stop` / `task.console_task_retry` / `task.console_subtask_stop` /
 * `task.console_subtask_retry`）。
 */
export const TASK_CENTER_READ_PERMISSION = 'task_center:read';
export const TASK_CENTER_WRITE_PERMISSION = 'task_center:write';
