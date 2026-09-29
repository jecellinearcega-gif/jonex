import { useEffect, useRef, useState } from 'react';
import {
  Button,
  Collapse,
  Divider,
  Drawer,
  Flex,
  Form,
  Input,
  Select,
  Space,
  Switch,
  Typography,
  message,
  theme,
} from 'antd';
import { ApiOutlined, SaveOutlined, SettingOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import SectionHeading from '../../components/SectionHeading';
import {
  INTERVAL_OPTIONS,
  getConnectionStatusMeta,
  readProbeTarget,
  testConnection,
  updateIntegrationConfig,
  type DatasourceConfigUpdate,
  type DatasourceStatus,
} from '../../api/identityIntegration';

/**
 * 「用户与组织同步配置」侧边抽屉（E2 保存 / E3 测试连接）。
 *
 * 基础配置（数据源名称 / 自动同步开关+说明 / 同步周期）与可折叠的高级配置
 * （接口地址 + 「测试连接」/ 认证方式 / 脱敏凭据），底部「取消」+「保存配置」。
 *
 * ⚠️ 后端没有返回完整配置的 GET 端点（E1 只给状态视图），接口地址与认证凭据无法回填。
 * 这两个字段留空即不提交，靠 E2 的部分更新语义保持原值（与只写的 `token` 一致）。
 * 数据源测试连接可以安全携带表单快照：后端 `_test_datasource` 只做探测，不落库。
 *
 * 认证方式一期固定 `Bearer Token`：E1 不返回 `auth_type`，前端无从回显真实值，
 * 故只做只读展示、不提交该字段，避免把存量值静默覆盖成另一个口径。
 */
const { Text } = Typography;
const { useToken } = theme;

/** 自动同步未配置过周期时的默认值（小时） */
const DEFAULT_INTERVAL_HOURS = 24;

interface SyncConfigFormValues {
  sourceName?: string;
  autoSync: boolean;
  cycle?: number;
  /** 留空 = 不修改（不传该字段） */
  endpoint?: string;
  /** 留空 = 不修改（不传该字段） */
  credential?: string;
}

interface SyncConfigDrawerProps {
  open: boolean;
  /** E1 的 datasource 段，作为表单初值与验证状态来源 */
  status: DatasourceStatus | null;
  onClose: () => void;
  /** 保存 / 测试成功后触发，由页面重新拉取 E1 */
  onChanged: () => void;
}

export default function SyncConfigDrawer({ open, status, onClose, onChanged }: SyncConfigDrawerProps) {
  const { t } = useTranslation();
  const { token } = useToken();
  const [form] = Form.useForm<SyncConfigFormValues>();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  // 同步周期只在自动同步开启时有意义，跟随开关显隐
  const autoSync = Form.useWatch('autoSync', form);

  // 带插值的键（testFailedWithTarget）需要透传 options，与 SSO 抽屉的 dl 同签名
  const dl = (key: string, options?: Record<string, unknown>) => t(`tenantManagement.syncDrawer.${key}`, options);

  // 仅在「打开」这一刻用最新的 E1 状态初始化表单；地址与凭据不回填（留空 = 不修改）。
  // 不能在 status 变化时重置：测试连接成功会触发页面 refreshAll，那会把用户刚填的内容清掉。
  const inited = useRef(false);
  useEffect(() => {
    if (open && !inited.current) {
      form.setFieldsValue({
        sourceName: status?.name ?? '',
        autoSync: status?.auto_sync ?? false,
        cycle: status?.interval ?? DEFAULT_INTERVAL_HOURS,
        endpoint: '',
        credential: '',
      });
    }
    inited.current = open;
  }, [open, status, form]);

  const onTestConnection = async () => {
    setTesting(true);
    try {
      const values = form.getFieldsValue();
      // 仅带上用户在表单里填写的项，未填的由后端回落到已存配置
      const snapshot: DatasourceConfigUpdate = {
        ...(values.endpoint ? { url: values.endpoint } : {}),
        ...(values.credential ? { token: values.credential } : {}),
      };
      await testConnection({ target: 'datasource', datasource: snapshot });
      message.success(dl('testOk'));
      onChanged();
    } catch (e: unknown) {
      // 超时 / 连接失败 / 认证失败由后端分类返回（5004/5005/5006），用后端 i18n 后的 message；
      // 5007 是「按同步契约逐条校验后判定返回的不是标准用户列表」（典型是接口地址指向门户首页
      // 或控制台页面），后端 message 是通用码文案，这里换一句能指导用户改地址的专属提示。
      const baseMessage =
        (e as { bizCode?: number } | null)?.bizCode === 5007
          ? dl('testFailedContract')
          : e instanceof Error && e.message
            ? e.message
            : dl('testFailed');
      // 失败文案回显后端**实际探了哪个地址**（errorDetails.target），否则界面显示 A、
      // 系统探 B 时用户无从得知。注意请求带的是表单快照，后端还可能回落到已存配置，
      // 两者分叉时只有后端回报的探测目标算数。
      // 后端未部署该字段时 errorDetails 缺失 → 回落到原来的中性文案。
      const probeTarget = readProbeTarget(e);
      message.error(
        probeTarget ? dl('testFailedWithTarget', { message: baseMessage, target: probeTarget }) : baseMessage,
      );
    } finally {
      setTesting(false);
    }
  };

  const onFinish = async (values: SyncConfigFormValues) => {
    setSaving(true);
    try {
      await updateIntegrationConfig({
        datasource: {
          ...(values.sourceName ? { name: values.sourceName } : {}),
          auto_sync: values.autoSync,
          // 关闭自动同步时周期不可见，此时提交会把后端原值静默换成表单里的默认档位，故不传该字段
          ...(values.autoSync && typeof values.cycle === 'number' ? { interval: values.cycle } : {}),
          ...(values.endpoint ? { url: values.endpoint } : {}),
          ...(values.credential ? { token: values.credential } : {}),
        },
      });
      message.success(t('common.saveSuccess'));
      onChanged();
      onClose();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : dl('saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const connection = status ? getConnectionStatusMeta(t, status.connection_status) : null;

  return (
    <Drawer
      title={dl('title')}
      open={open}
      onClose={onClose}
      width={560}
      footer={
        <Flex justify="space-between" align="center">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => form.submit()}>
            {dl('save')}
          </Button>
        </Flex>
      }
    >
      <Form<SyncConfigFormValues> form={form} layout="vertical" requiredMark={false} onFinish={onFinish}>
        {/* 分区一：基础配置 */}
        <SectionHeading icon={<SettingOutlined />} color={token.colorPrimary}>
          {dl('groupBasic')}
        </SectionHeading>
        <Form.Item name="sourceName" label={dl('sourceName')} style={{ marginTop: 16 }}>
          <Input allowClear placeholder={dl('sourceName')} />
        </Form.Item>

        {/* 自动同步开关行：浅色高亮卡，聚焦主开关与说明 */}
        <div
          style={{
            background: token.colorFillTertiary,
            borderRadius: token.borderRadiusLG,
            padding: `${token.paddingXS}px ${token.paddingSM}px`,
          }}
        >
          {/* Form.Item 只管 Switch：它把 value/onChange 注入唯一子节点，
              包住 Flex 会让注入落到 div 上，开关就只是自己变色、表单值不变。 */}
          <Flex justify="space-between" align="center">
            <Flex vertical gap={2}>
              <Text strong>{dl('autoSync')}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {dl('autoSyncDesc')}
              </Text>
            </Flex>
            <Form.Item name="autoSync" valuePropName="checked" noStyle>
              <Switch />
            </Form.Item>
          </Flex>
        </div>
        {autoSync ? (
          <Form.Item name="cycle" label={dl('cycle')} style={{ marginTop: 14 }}>
            <Select
              options={INTERVAL_OPTIONS.map((item) => ({
                value: item.value,
                label: dl(item.labelKey),
              }))}
            />
          </Form.Item>
        ) : null}

        <Divider style={{ margin: `${token.margin}px 0 ${token.marginLG}px` }} />

        {/* 分区二：高级配置（可折叠，默认展开；接口/凭据等不常用项收敛保持表单轻量） */}
        <Collapse
          ghost
          defaultActiveKey={['advanced']}
          expandIconPlacement="end"
          // 折叠面板去掉默认左右内边距，标题与展开字段都对齐基础配置的字段左缘
          styles={{
            header: { padding: '8px 0' },
            body: { paddingTop: 2, paddingBottom: 0, paddingInline: 0 },
          }}
          items={[
            {
              key: 'advanced',
              label: (
                <SectionHeading icon={<ApiOutlined />} color={token.colorPrimary}>
                  {dl('groupAdvanced')}
                </SectionHeading>
              ),
              children: (
                <>
                  <Text type="secondary" style={{ display: 'block', fontSize: 13, marginBottom: 18 }}>
                    {dl('advancedHint')}
                  </Text>
                  <Form.Item
                    name="endpoint"
                    label={dl('endpoint')}
                    rules={[
                      {
                        // 留空 = 不修改，放行；非空必须带 http(s):// 前缀。
                        // 后端 `validate_datasource_url` 也会拦（1007），此处前置到表单少一次往返。
                        validator: (_: unknown, value?: string) =>
                          !value || /^https?:\/\//i.test(value)
                            ? Promise.resolve()
                            : Promise.reject(new Error(dl('endpointInvalid'))),
                      },
                    ]}
                  >
                    <Space.Compact style={{ width: '100%' }}>
                      <Input allowClear placeholder={dl('keepUnchanged')} />
                      <Button loading={testing} onClick={onTestConnection}>
                        {dl('testConn')}
                      </Button>
                    </Space.Compact>
                  </Form.Item>
                  {/* 认证方式一期固定，只读展示；`Bearer Token` 为产品术语，各语言界面保持一致原文 */}
                  <Form.Item label={dl('authType')}>
                    <Input value="Bearer Token" disabled />
                  </Form.Item>
                  <Form.Item name="credential" label={dl('credential')}>
                    <Input.Password autoComplete="new-password" placeholder={dl('credentialPlaceholder')} />
                  </Form.Item>

                  {/* 连接状态：E1 的派生三态（未配置 / 已验证 / 未验证）。
                      `unverified` = 有地址但验证失效 —— 改过地址或凭据就会落到这里
                      （后端变更 url/token 时清空 verified_at），故补一行显式重测引导，
                      否则用户只看到状态标签、不知道要做什么。
                      `unconfigured` 时下面的字段本就全空，再挂一行「未配置」只是噪音，不显示。 */}
                  {connection && status?.connection_status !== 'unconfigured' ? (
                    <Text type={connection.type} style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
                      {connection.label}
                    </Text>
                  ) : null}
                  {status?.connection_status === 'unverified' ? (
                    <Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
                      {dl('connUnverifiedHint')}
                    </Text>
                  ) : null}
                </>
              ),
            },
          ]}
        />
      </Form>
    </Drawer>
  );
}
