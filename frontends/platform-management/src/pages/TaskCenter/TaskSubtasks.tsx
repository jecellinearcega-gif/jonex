import { useCallback, useEffect, useState } from 'react';
import { Button, Empty, Space, Spin, Tag, Timeline, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { listSubtasks } from '../../api/taskCenter';
import type { SubtaskConsoleItem, TaskStatus } from '../../api/taskCenter';
import { SUBTASK_STATUS_COLORS, SUBTASK_STATUS_DOT_COLORS, SUBTASK_STATUS_LABEL_KEY, subtaskAction } from './constant';
import { fmtDateTime, fmtDuration } from './format';

/**
 * 子任务动作的回调。带任务 id 是因为同一套回调同时服务展开行与抽屉（两个不同的任务）；
 * 带名字是为了弹窗标题能指名道姓（`node_id` 用户看不懂）。
 */
export type SubtaskActionHandler = (taskId: string, nodeId: string, name: string) => void;

interface Props {
  taskId: string;
  /** 任务当前状态：子任务能给什么动作由它 + 子任务自身状态共同决定。 */
  taskStatus: TaskStatus;
  /** 有没有 `task_center:write`。没有就连链接都不渲染。 */
  canWrite: boolean;
  /**
   * 父组件在每次写操作后 +1，用来触发这里重取 —— `CommonTable.refresh()` 只刷表格行，
   * 不会重新挂载展开行的子节点（`rowKey` 稳定），所以刷新得靠这个信号显式传下来。
   */
  reloadToken: number;
  onStopSubtask: SubtaskActionHandler;
  onRetrySubtask: SubtaskActionHandler;
  indent?: number;
}

/**
 * 子任务列表（展开行用）。挂载即取数 —— 展开行只在展开时渲染、抽屉只在打开时渲染，
 * 所以这天然是懒加载，不需要额外缓存（同一个任务两处都打开时多取一次的成本可以忽略）。
 *
 * 不传 `attempt`：接口支持 `?attempt=N`，但列表接口返回的任务对象里没有任何 attempt
 * 信息，页面无从知道该传第几次，走「最新一次运行」的默认。
 */
export default function TaskSubtasks({
  taskId,
  taskStatus,
  canWrite,
  reloadToken,
  onStopSubtask,
  onRetrySubtask,
  indent = 48,
}: Props) {
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
    // 卸载后不再写 state：展开行折叠、抽屉关闭都会走到这里
    return () => {
      cancelled = true;
    };
    // reloadToken 是「该重取了」的触发信号，函数体里读不到它，所以 lint 会误判成多余依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, reloadToken]);

  useEffect(() => load(), [load]);

  if (loading) {
    return (
      <div style={{ padding: 16, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }

  if (error) {
    return (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.loadFailed')}>
        <Button onClick={load}>{t('common.retry')}</Button>
      </Empty>
    );
  }

  if (!items.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noData')} />;
  }

  return (
    <div style={{ marginLeft: Math.max(0, indent - 18), paddingLeft: 16 }}>
      <Timeline
        items={items.map((item) => {
          const action = canWrite ? subtaskAction(taskStatus, item.status) : null;
          const name = item.subtask_name ?? item.node_id;
          return {
            // status 传不进来（useItems 按 loading 覆写它），所以「处理中」走 loading 这条语义通道，
            // 其余状态只能靠点色区分
            loading: item.status === 'running',
            color: SUBTASK_STATUS_DOT_COLORS[item.status],
            content: (
              <Space size={12}>
                <span>{item.subtask_name ?? '-'}</span>
                <Tag color={SUBTASK_STATUS_COLORS[item.status]}>{t(SUBTASK_STATUS_LABEL_KEY[item.status])}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {fmtDateTime(item.started_at)}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {fmtDuration(item.duration_seconds)}
                </Typography.Text>
                {/* 动作回调带的是 node_id（写接口的寻址键），不是 item.id */}
                {action === 'stop' && (
                  <Typography.Link onClick={() => onStopSubtask(taskId, item.node_id, name)}>
                    {t('taskCenter.action.stop')}
                  </Typography.Link>
                )}
                {action === 'retry' && (
                  <Typography.Link onClick={() => onRetrySubtask(taskId, item.node_id, name)}>
                    {t('taskCenter.action.retry')}
                  </Typography.Link>
                )}
              </Space>
            ),
          };
        })}
      />
    </div>
  );
}
