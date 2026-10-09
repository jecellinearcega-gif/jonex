import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Collapse,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Modal,
  Result,
  Row,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
  message,
  theme,
} from 'antd';
import type { GlobalToken, TableColumnsType } from 'antd';
import { CopyOutlined, ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { useTranslation } from 'react-i18next';
import { CommonTable, PageContent, PageHeader, usePermission } from '@jonex/shared-lib';
import type { CommonTableRef, FilterConfig, FilterContext, TableQueryParams } from '@jonex/shared-lib';
import { onSpaceChanged, readPersistedSpaceId } from '@jonex/shell-sdk';
import {
  TASK_CENTER_READ_PERMISSION,
  TASK_CENTER_WRITE_PERMISSION,
  listSubtasks,
  listTasks,
  retrySubtask,
  retryTask,
  stopSubtask,
  stopTask,
} from '../../api/taskCenter';
import type { SubtaskConsoleItem, TaskConsoleItem, TaskStatus, TaskWriteResult } from '../../api/taskCenter';
import TaskSubtasks from './TaskSubtasks';
import type { SubtaskActionHandler } from './TaskSubtasks';
import { fmtDateTime, fmtDuration } from './format';
import {
  SUBTASK_STATUS_COLORS,
  SUBTASK_STATUS_LABEL_KEY,
  TASK_SOURCE_LABEL_KEY,
  TASK_STATUS_COLORS,
  TASK_STATUS_LABEL_KEY,
  TASK_STATUS_ORDER,
  TASK_TYPE_LABEL_KEY,
  isTaskTerminal,
  subtaskAction,
} from './constant';
import type { TaskFilters } from './types';

const { RangePicker } = DatePicker;

/** 「全部」在筛选值里的表示。空串让取数逻辑走「不传 status」分支。 */
const FILTER_ALL = '';

/**
 * 筛选区的 label 定宽、控件吃满其余空间。取值照抄 CommonTable 内部那对常量 ——
 * shared-lib 没导出它们，不照抄的话这个控件的 label 会按内容自适应，
 * 与同一行 keyword 的定宽 label 对不齐。
 */
const FILTER_LABEL_COL = { flex: '0 0 5.5em', style: { textAlign: 'right' as const } };
const FILTER_WRAPPER_COL = { flex: '1 1 0%', style: { minWidth: 0 } };

/** 复制按钮。任务编号与每个子任务编号共用，四处同一份交互。 */
function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const onCopy = async (): Promise<void> => {
    try {
      // clipboard 在非安全上下文（http 非 localhost）不可用，失败要有反馈而不是静默
      await navigator.clipboard.writeText(text);
      message.success(t('common.copySuccess'));
    } catch {
      message.error(t('common.copyFailed'));
    }
  };
  return <Button type="text" size="small" icon={<CopyOutlined />} onClick={onCopy} aria-label={t('common.copy')} />;
}

export default function TaskCenter() {
  const { t } = useTranslation();
  // 权限来自 shell 下发的 permissions（hosted / standalone 两种形态都由 ShellContext 提供）
  const { hasPerm } = usePermission();
  const canRead = hasPerm(TASK_CENTER_READ_PERMISSION);
  // 一个码管四个写 action（停止任务 / 重跑任务 / 停止某步 / 重跑某步），所以叫 write 不叫 stop
  const canWrite = hasPerm(TASK_CENTER_WRITE_PERMISSION);
  const { token } = theme.useToken();
  const tableRef = useRef<CommonTableRef>(null);
  const [detail, setDetail] = useState<TaskConsoleItem | null>(null);
  /**
   * 抽屉里那条任务的最新状态。
   *
   * 写接口回的是内部契约 `TaskItemResponse`（**没有 `duration_seconds`**），与列表演示用的
   * `TaskConsoleItem` 不是同一个形状，所以**不能整份塞进 `detail`**。这里只取 `status`：
   * 门禁要的就是它。代价是本次操作后抽屉的 `finished_at` / `duration_seconds` 短暂偏旧，
   * 关掉重开即新。
   */
  const [detailStatus, setDetailStatus] = useState<TaskStatus | null>(null);
  /**
   * 写操作后的重取信号。`CommonTable.refresh()` 只重取表格行，**不会重新挂载展开行里的
   * 子节点**（`rowKey` 稳定），所以展开行与抽屉里的子任务列表得另有一个信号才刷得动。
   * 全局共用一个：任何一次写都可能改变任意行的状态。
   */
  const [subtaskReloadToken, setSubtaskReloadToken] = useState(0);
  /** 各状态数量由接口随列表一并返回；标签栏的角标读它。 */
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  /**
   * 当前空间。**取自主应用（Shell）的空间切换器**，页面上不再有自己的空间下拉。
   *
   * 跨应用取值走 shell-sdk 的统一通道（localStorage + 事件），hosted 的 iframe 与
   * standalone 的跨标签页两种形态都覆盖 —— 这里是本页唯一的跨应用取值。
   */
  const [spaceId, setSpaceId] = useState<string | null>(() => readPersistedSpaceId());

  useEffect(() => onSpaceChanged(setSpaceId), []);

  /**
   * 空间变了要重取一次。**跳过首次**：挂载时 CommonTable 自己已经在取第一份数，
   * 不跳过就会连发两次请求。
   *
   * 取数 effect 观测的是查询条件（页/筛选值），闭包里的 `fetch` 走 ref 不进依赖，
   * 所以空间变化不会自动触发取数 —— 必须显式 refresh 一次；refresh 时读到的是最新
   * 渲染的 `fetchRows`，`spaceId` 也就是新值。
   */
  const spaceReadyRef = useRef(false);
  useEffect(() => {
    if (!spaceReadyRef.current) {
      spaceReadyRef.current = true;
      return;
    }
    tableRef.current?.refresh();
  }, [spaceId]);

  const fetchRows = useCallback(
    async ({ page, pageSize, keyword, task_type, source, range, status }: TableQueryParams<TaskFilters>) => {
      const res = await listTasks({
        page,
        page_size: pageSize,
        keyword: keyword || undefined,
        // 空间来自主应用（页面自己那份 state），另有三个筛选参数后端目前不接收
        // （见 TaskListParams 的注释），照发以备补齐
        space_id: spaceId || undefined,
        task_type: task_type || undefined,
        source: source || undefined,
        // 区间取「当天 00:00:00」到「当天 23:59:59.999」，与同子应用 OperationLog 的口径一致
        start_time: range?.[0]?.startOf('day').toISOString(),
        end_time: range?.[1]?.endOf('day').toISOString(),
        // 接口的 status 是多选；页面当前是单选，传单元素数组
        status: status ? [status as TaskStatus] : undefined,
      });
      setStatusCounts(res.status_counts ?? {});
      return { list: res.items ?? [], total: res.total ?? 0 };
    },
    [spaceId],
  );

  /**
   * 取数失败的报警。CommonTable 只清空列表、不再自己渲染「加载失败」兜底，
   * 表格会照常显示成空态 —— 少了这条提示，「接口挂了」与「确实没有任务」在页面上就分不出来。
   */
  const handleListError = useCallback(
    (e: unknown) => {
      message.error(e instanceof Error && e.message ? e.message : t('common.loadFailed'));
    },
    [t],
  );

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('taskCenter.filter.keyword'),
        type: 'input',
        placeholder: t('taskCenter.filter.keywordPlaceholder'),
      },
      {
        key: 'task_type',
        label: t('taskCenter.filter.type'),
        type: 'select',
        placeholder: t('taskCenter.filter.typeAll'),
        options: Object.entries(TASK_TYPE_LABEL_KEY).map(([value, key]) => ({ value, label: t(key) })),
      },
      {
        key: 'source',
        label: t('taskCenter.filter.source'),
        type: 'select',
        placeholder: t('taskCenter.filter.sourceAll'),
        options: Object.entries(TASK_SOURCE_LABEL_KEY).map(([value, key]) => ({ value, label: t(key) })),
      },
    ],
    [t],
  );

  /**
   * 时间区间。`RangePicker` 是配置项表达不了的控件，故走 filterExtra。
   *
   * 值写进 FilterContext 而不是页面自己的 state：CommonTable 的「重置」只清它，
   * 页面 state 不在它的视野里，用 state 会让控件漏在重置之外、点完重置还留着旧值。
   */
  const renderRange = useCallback(
    (ctx: FilterContext<TaskFilters>) => (
      <Form.Item
        label={t('taskCenter.filter.range')}
        labelCol={FILTER_LABEL_COL}
        wrapperCol={FILTER_WRAPPER_COL}
        style={{ marginBottom: 0 }}
      >
        <RangePicker
          value={ctx.get('range') ?? null}
          onChange={(v) => ctx.set('range', (v as [dayjs.Dayjs, dayjs.Dayjs] | null) ?? null)}
          allowClear
          style={{ width: '100%' }}
        />
      </Form.Item>
    ),
    [t],
  );

  /**
   * 状态标签栏。计数直接用接口给的 `status_counts` —— 它的口径就是「只受除 status 外的
   * 筛选条件影响」，与标签栏的语义逐字一致，页面不需要自己算。
   *
   * 这一行同时承载「刷新 / 重置」：分类靠左、按钮靠右。按钮因此不能再由 CommonTable
   * 渲染（内置那对固定在筛选行右侧），改成本页自己摆 —— 「刷新」直接调 ref，「重置」
   * 走组件新开的 `ref.reset()`（它比 `ctx.set` 逐 key 清多一步：把输入框尚未落地的
   * 防抖值一并作废），所以要让 `actions` 关掉内置那对，免得两处并存。
   */
  const renderStatusBar = useCallback(
    (ctx: FilterContext<TaskFilters>) => {
      const countOf = (status: TaskStatus): number => statusCounts[status] ?? 0;
      const all = TASK_STATUS_ORDER.reduce((sum, s) => sum + countOf(s), 0);
      return (
        <Row justify="space-between" align="middle" wrap={false}>
          <Segmented
            value={ctx.get('status') ?? FILTER_ALL}
            onChange={(value) => ctx.set('status', value as string)}
            options={[
              { label: `${t('taskCenter.status.all')} ${all}`, value: FILTER_ALL },
              ...TASK_STATUS_ORDER.map((status) => ({
                label: `${t(TASK_STATUS_LABEL_KEY[status])} ${countOf(status)}`,
                value: status,
              })),
            ]}
          />
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => tableRef.current?.refresh()}>
              {t('common.refresh')}
            </Button>
            <Button onClick={() => tableRef.current?.reset()}>{t('common.reset')}</Button>
          </Space>
        </Row>
      );
    },
    [statusCounts, t],
  );

  /** 打开详情：连状态一起重置，免得抽屉里残留上一条任务操作后的 status。 */
  const openDetail = useCallback((row: TaskConsoleItem) => {
    setDetail(row);
    setDetailStatus(null);
  }, []);

  const closeDetail = useCallback(() => {
    setDetail(null);
    setDetailStatus(null);
  }, []);

  const refreshAll = useCallback(() => {
    tableRef.current?.refresh();
    setSubtaskReloadToken((n) => n + 1);
  }, []);

  /**
   * 把写操作的返回值同步给抽屉 —— **只取 `status`**（理由见 `detailStatus` 的注释）。
   * 抽屉开着的可能是别的任务，按 id 对上才写。
   */
  const syncDetailStatus = useCallback(
    (res: TaskWriteResult) => {
      if (detail?.id === res.id) setDetailStatus(res.status);
    },
    [detail],
  );

  /**
   * 四个写操作的共同骨架：二次确认 → 调用 → 提示 → 刷新。
   *
   * `onOk` 里**必须自己吞掉异常**：抛出去会让 antd 的确认框卡在 loading 不关。
   * 失败也刷新 —— 拒绝往往意味着页面上的状态已经旧了，重取一次让用户看到真实状态。
   */
  const runWrite = useCallback(
    (title: string, run: () => Promise<TaskWriteResult>, successText: string, failedText: string) => {
      Modal.confirm({
        title,
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        onOk: async () => {
          try {
            const res = await run();
            message.success(successText);
            syncDetailStatus(res);
          } catch (e: unknown) {
            // 后端的 message 已按 locale 译好（如「上游未全部成功」），直接展示，不自己区分码
            message.error(e instanceof Error && e.message ? e.message : failedText);
          } finally {
            refreshAll();
          }
        },
      });
    },
    [t, refreshAll, syncDetailStatus],
  );

  const handleStop = useCallback(
    (row: TaskConsoleItem) => {
      runWrite(
        t('taskCenter.action.stopConfirm'),
        () => stopTask(row.id),
        t('taskCenter.action.stopped'),
        t('taskCenter.action.stopFailed'),
      );
    },
    [t, runWrite],
  );

  const handleRetry = useCallback(
    (row: TaskConsoleItem) => {
      runWrite(
        t('taskCenter.action.retryConfirm'),
        () => retryTask(row.id),
        t('taskCenter.action.retried'),
        t('taskCenter.action.retryFailed'),
      );
    },
    [t, runWrite],
  );

  // 子任务的两个动作：nodeId 是**节点名**（写接口的寻址键），name 只用来指名道姓
  const handleStopSubtask = useCallback<SubtaskActionHandler>(
    (taskId, nodeId, name) => {
      runWrite(
        t('taskCenter.action.subtaskStopConfirm', { name }),
        () => stopSubtask(taskId, nodeId),
        t('taskCenter.action.subtaskStopped'),
        t('taskCenter.action.subtaskStopFailed'),
      );
    },
    [t, runWrite],
  );

  const handleRetrySubtask = useCallback<SubtaskActionHandler>(
    (taskId, nodeId, name) => {
      runWrite(
        t('taskCenter.action.subtaskRetryConfirm', { name }),
        () => retrySubtask(taskId, nodeId),
        t('taskCenter.action.subtaskRetried'),
        t('taskCenter.action.subtaskRetryFailed'),
      );
    },
    [t, runWrite],
  );

  const columns = useMemo<TableColumnsType<TaskConsoleItem>>(
    () => [
      {
        title: t('taskCenter.column.name'),
        key: 'name',
        render: (_, row) => (
          <>
            {/* 任务名允许为空（发起时没给），空就显示 '-'，不拿 id 顶替——两行各有各的信息 */}
            <div>{row.task_name || '-'}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {row.id}
            </Typography.Text>
          </>
        ),
      },
      {
        title: t('common.status'),
        key: 'status',
        render: (_, row) => <Tag color={TASK_STATUS_COLORS[row.status]}>{t(TASK_STATUS_LABEL_KEY[row.status])}</Tag>,
      },
      {
        title: t('taskCenter.column.startedAt'),
        key: 'startedAt',
        render: (_, row) => fmtDateTime(row.started_at),
      },
      {
        title: t('taskCenter.column.finishedAt'),
        key: 'finishedAt',
        render: (_, row) => fmtDateTime(row.finished_at),
      },
      {
        title: t('taskCenter.column.duration'),
        key: 'duration',
        render: (_, row) => fmtDuration(row.duration_seconds),
      },
      {
        title: t('common.actions'),
        key: 'actions',
        render: (_, row) => (
          <Space size={8}>
            <a onClick={() => openDetail(row)}>{t('taskCenter.action.detail')}</a>
            {/* 只有还没结束的任务能停：接口对已在跑但没有 pending 子任务的任务会拒绝。
                写权限另外判：能进本页只说明有 read 码，写操作是 task_center:write */}
            {canWrite && (row.status === 'pending' || row.status === 'running') && (
              <a style={{ color: token.colorError }} onClick={() => handleStop(row)}>
                {t('taskCenter.action.stop')}
              </a>
            )}
            {/* 重跑整条任务：前置就是「任务已终态」，与后端 `_require_terminal` 逐字对应 */}
            {canWrite && isTaskTerminal(row.status) && (
              <a onClick={() => handleRetry(row)}>{t('taskCenter.action.retry')}</a>
            )}
          </Space>
        ),
      },
    ],
    [t, token, openDetail, handleStop, handleRetry, canWrite],
  );

  // 路由守卫在两个壳里不等价（standalone 的 basename 带尾斜杠，拦不住），页面自己兜一道
  if (!canRead) return <Result status="403" title="403" subTitle={t('error.403')} />;

  return (
    <>
      <PageHeader title={t('taskCenter.title')} />
      <PageContent>
        <CommonTable<TaskConsoleItem, TaskFilters>
          ref={tableRef}
          filters={filters}
          filterExtra={renderRange}
          filterExtraRow={renderStatusBar}
          // 刷新 / 重置跟着状态标签栏走（见 renderStatusBar），内置那对关掉
          actions={{ refresh: false, reset: false }}
          columns={columns}
          rowKey="id"
          size="middle"
          expandable={{
            expandedRowRender: (row) => (
              <TaskSubtasks
                taskId={row.id}
                taskStatus={row.status}
                canWrite={canWrite}
                reloadToken={subtaskReloadToken}
                onStopSubtask={handleStopSubtask}
                onRetrySubtask={handleRetrySubtask}
              />
            ),
          }}
          fetch={fetchRows}
          onError={handleListError}
        />
      </PageContent>
      <Drawer open={Boolean(detail)} onClose={closeDetail} title={t('taskCenter.detail.title')} size={680}>
        {detail && (
          <Space direction="vertical" size={20} style={{ display: 'flex' }}>
            {/* 标签在上、值在下：两列网格里标签与值等宽排布，横排会看不出字段边界 */}
            <Descriptions column={2} layout="vertical" size="small" title={t('taskCenter.detail.basic')}>
              <Descriptions.Item label={t('taskCenter.column.name')}>{detail.task_name || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('taskCenter.detail.taskId')}>
                <Space size={0}>
                  {detail.id}
                  <CopyButton text={detail.id} />
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>
                {/* 写操作刚回来的状态优先：停某一步会把整条任务收口成 failed */}
                <Tag color={TASK_STATUS_COLORS[detailStatus ?? detail.status]}>
                  {t(TASK_STATUS_LABEL_KEY[detailStatus ?? detail.status])}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('taskCenter.column.startedAt')}>
                {fmtDateTime(detail.started_at)}
              </Descriptions.Item>
              <Descriptions.Item label={t('taskCenter.column.finishedAt')}>
                {fmtDateTime(detail.finished_at)}
              </Descriptions.Item>
              <Descriptions.Item label={t('taskCenter.detail.totalDuration')}>
                {fmtDuration(detail.duration_seconds)}
              </Descriptions.Item>
            </Descriptions>

            <Divider style={{ margin: 0 }} />

            <div>
              <Typography.Title level={5} style={{ marginTop: 0 }}>
                {t('taskCenter.detail.subtasks')}
              </Typography.Title>
              <DrawerSubtasks
                taskId={detail.id}
                taskStatus={detailStatus ?? detail.status}
                canWrite={canWrite}
                reloadToken={subtaskReloadToken}
                onStopSubtask={handleStopSubtask}
                onRetrySubtask={handleRetrySubtask}
                token={token}
              />
            </div>
          </Space>
        )}
      </Drawer>
    </>
  );
}

/**
 * 抽屉里的子任务卡片。与展开行的 Timeline 展示同一份数据、不同的形态：
 * 抽屉要给出子任务编号与两个时刻，折叠起来才装得下。
 *
 * 不复用 TaskSubtasks：那个是 Timeline、且不需要编号；两处共用的只是取数，
 * 为这 20 行抽一个 hook 会多引一个文件，不值得（若出现第三处就该抽了）。
 */
function DrawerSubtasks({
  taskId,
  taskStatus,
  canWrite,
  reloadToken,
  onStopSubtask,
  onRetrySubtask,
  token,
}: {
  taskId: string;
  taskStatus: TaskStatus;
  canWrite: boolean;
  reloadToken: number;
  onStopSubtask: SubtaskActionHandler;
  onRetrySubtask: SubtaskActionHandler;
  token: GlobalToken;
}) {
  const { t } = useTranslation();
  const [items, setItems] = useState<SubtaskConsoleItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listSubtasks(taskId)
      .then((rows) => {
        if (!cancelled) setItems(rows ?? []);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setItems([]);
          setError(e);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // reloadToken 是「该重取了」的触发信号，函数体里读不到它，所以 lint 会误判成多余依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, reloadToken]);

  useEffect(() => load(), [load]);

  if (error) {
    return (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.loadFailed')}>
        <Button onClick={load}>{t('common.retry')}</Button>
      </Empty>
    );
  }
  // 只在「还没有数据」时整块换 Spin：写操作后每次都要重载，若 loading 就把列表整个卸载，
  // 用户展开着的那张卡片会被折叠回去
  if (loading && !items.length) return <Spin />;
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noData')} />;

  return (
    // 默认不展开：子任务条数由编排定义决定（两步或三步），不像原来固定三个，
    // 全展开会让抽屉长到不可读
    <Collapse
      items={items.map((item) => {
        const action = canWrite ? subtaskAction(taskStatus, item.status) : null;
        const name = item.subtask_name ?? item.node_id;
        return {
          // key 用行 id（React 需要一个稳定键）；node_id 只进写接口的 URL
          key: item.id,
          // 状态与耗时贴右、名称贴左：卡片头的两端对齐靠中间的弹性空档撑开
          label: (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1 }}>
              <span>{item.subtask_name || '-'}</span>
              <div style={{ flex: 1 }} />
              <Tag color={SUBTASK_STATUS_COLORS[item.status]}>{t(SUBTASK_STATUS_LABEL_KEY[item.status])}</Tag>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {fmtDuration(item.duration_seconds)}
              </Typography.Text>
            </div>
          ),
          children: (
            <Space direction="vertical" size={12} style={{ display: 'flex' }}>
              {/* 子任务编号横跨两列：它比两个时刻加起来还长，挤进一列必折行 */}
              <Descriptions column={2} layout="vertical" size="small">
                <Descriptions.Item label={t('taskCenter.detail.subtaskId')} span={2}>
                  <Space size={0}>
                    {item.id}
                    <CopyButton text={item.id} />
                  </Space>
                </Descriptions.Item>
                <Descriptions.Item label={t('taskCenter.column.startedAt')}>
                  {fmtDateTime(item.started_at)}
                </Descriptions.Item>
                <Descriptions.Item label={t('taskCenter.column.finishedAt')}>
                  {fmtDateTime(item.finished_at)}
                </Descriptions.Item>
              </Descriptions>

              <div
                style={{
                  background: token.colorFillQuaternary,
                  borderRadius: token.borderRadius,
                  padding: '8px 12px',
                }}
              >
                <Typography.Text type="secondary">{t('taskCenter.detail.result')}：</Typography.Text>
                <Typography.Text strong>
                  {t(`taskCenter.result.${item.status}`, { name: item.subtask_name || '-' })}
                </Typography.Text>
              </div>

              {/* 动作放在 children 里、不放 label：label 整块都是展开/折叠的点击区，
                  按钮挤进去点一下会连带把卡片收起来 */}
              {action && (
                <Space size={12}>
                  {action === 'stop' && (
                    <Button size="small" danger onClick={() => onStopSubtask(taskId, item.node_id, name)}>
                      {t('taskCenter.action.stop')}
                    </Button>
                  )}
                  {action === 'retry' && (
                    <Button size="small" onClick={() => onRetrySubtask(taskId, item.node_id, name)}>
                      {t('taskCenter.action.retry')}
                    </Button>
                  )}
                </Space>
              )}
            </Space>
          ),
        };
      })}
    />
  );
}
