import { useState } from 'react';
import { Alert, Flex, Modal, Radio, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { getSyncTypeLabel, triggerSync, type SyncResponse, type SyncType } from '../../api/identityIntegration';

/**
 * 「立即同步」二次选择弹窗（E4）。
 *
 * 增量 / 全量的业务影响差别大，同步前必须显式选择，不默认替用户拍板。
 *
 * ⚠️ E4 在请求内同步执行，返回的 `status` **就是终态**；且连接、认证、契约类失败
 * 不以错误码抛出，而是任务落 `failed` 后正常返回 —— 因此结果判定必须看 `status`，
 * 不能只靠 try/catch。
 *
 * 波 3 闸门（甲案）：`connection_status === 'unverified'` 的租户默认不许同步。
 * **拦截点全在本弹窗内部** —— 卡片上的「立即同步」按钮保持可点（只有 `unconfigured`
 * 才置灰），点开后由这里的红字警告 + 「仍要同步」文案完成二次确认，确认后才传 `force`。
 * ⛔ 不要把这个拦截挪到按钮的 `disabled` 上。
 */
const { Text } = Typography;

interface SyncNowModalProps {
  open: boolean;
  onClose: () => void;
  /**
   * 同步结束后触发。带回响应体：部分失败/失败时页面据 `task_id` 直接打开 E6 详情，
   * 无需用户再去列表里找那一次记录。
   */
  onDone: (res: SyncResponse) => void;
  /**
   * 收到「同步进行中」冲突时触发（后端 SyncTaskStateError，code 4005 / HTTP 409）。
   * 冲突说明已有另一次同步在跑，页面上的记录与配置状态都已过期，需要重拉。
   */
  onConflict?: () => void;
  /**
   * 波 3 闸门：`datasource.connection_status === 'unverified'`（有地址但未通过连接测试）。
   *
   * 与后端门控同一判据（`datasource_connection_verified_at IS NULL`）：为真时弹窗出红字警告、
   * 主按钮变「仍要同步」，点击即接受绕过（请求带 `force=true`）。
   * `unconfigured` 走不到本弹窗（卡片按钮已置灰），故只需处理这一档。
   */
  unverified?: boolean;
}

export default function SyncNowModal({ open, onClose, onDone, onConflict, unverified = false }: SyncNowModalProps) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<SyncType>('incremental');
  const [running, setRunning] = useState(false);

  const dl = (key: string) => t(`tenantManagement.syncDrawer.${key}`);

  const onSync = async () => {
    setRunning(true);
    try {
      // 未通过连接测试时，点到这一刻就是用户对红字警告的显式确认 —— 这才传 force 绕过闸门。
      const res = await triggerSync(mode, unverified);
      if (res.status === 'success') message.success(dl('syncDone'));
      else if (res.status === 'partial_failed') message.warning(dl('syncPartial'));
      else message.error(dl('syncFailed'));
      onDone(res);
      onClose();
    } catch (e: unknown) {
      // ⚠️ 顺序不能反：5008（未通过连接测试）的 HTTP 状态码同样是 409，所以
      // 「同步进行中」（4005 / 裸 409）这一档必须**先**判 —— 反了会把冲突误报成「未测通」。
      const err = e as { status?: number; bizCode?: number } | null;
      if (err?.status === 409 || err?.bizCode === 4005) onConflict?.();
      // 5008 走后端通用码文案没有指导性，换成「先去测连接」的专属提示（照 SSO 抽屉的 5007 写法）。
      // 兜底仍取后端 i18n 后的 message：错误信息比前端文案更具体时不该被盖掉。
      const baseMessage =
        err?.bizCode === 5008
          ? dl('syncNeedVerify')
          : e instanceof Error && e.message
            ? e.message
            : dl('syncFailed');
      message.error(baseMessage);
    } finally {
      setRunning(false);
    }
  };

  const modeOption = (value: SyncType, descKey: string) => (
    <Radio value={value}>
      <Flex vertical>
        <span>{getSyncTypeLabel(t, value)}</span>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {dl(descKey)}
        </Text>
      </Flex>
    </Radio>
  );

  return (
    <Modal
      title={dl('syncNowTitle')}
      open={open}
      onOk={onSync}
      onCancel={onClose}
      okText={unverified ? dl('forceSync') : t('common.confirm')}
      cancelText={t('common.cancel')}
      okButtonProps={{ loading: running }}
      cancelButtonProps={{ disabled: running }}
      maskClosable={!running}
      closable={!running}
    >
      <Flex vertical gap={8}>
        {/* 闸门警告：未通过连接测试时红字置顶，主按钮据此变「仍要同步」= 二次确认 */}
        {unverified ? <Alert type="error" showIcon message={dl('syncNeedVerify')} /> : null}
        <Text type="secondary" style={{ fontSize: 13 }}>
          {dl('syncMode')}
        </Text>
        {/* 同步进行中禁止改模式，避免「提交的是 A、看到的是 B」 */}
        <Radio.Group
          value={mode}
          onChange={(e) => setMode(e.target.value as SyncType)}
          disabled={running}
          style={{ width: '100%' }}
        >
          <Flex vertical gap={12}>
            {modeOption('incremental', 'modeIncrementalDesc')}
            {modeOption('full', 'modeFullDesc')}
          </Flex>
        </Radio.Group>
        {running ? <Alert type="info" showIcon message={dl('syncRunning')} style={{ marginTop: 4 }} /> : null}
      </Flex>
    </Modal>
  );
}
