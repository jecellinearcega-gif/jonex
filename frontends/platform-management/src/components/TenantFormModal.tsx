import React, { forwardRef, useImperativeHandle, useState } from 'react';
import { Form, Input, Modal, Switch, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  createTenant,
  updateTenant,
  type TenantCreatePayload,
  type TenantItem,
  type TenantUpdatePayload,
} from '../api/tenants';

export interface TenantFormModalRef {
  openCreate: () => void;
  openEdit: (item: TenantItem) => void;
}

const { Text } = Typography;

interface TenantFormValues {
  id?: string;
  name: string;
  description?: string;
  /** 仅编辑态可改 */
  status?: boolean;
}

/**
 * 租户新建 / 编辑弹窗（列表页与详情页共用）。
 *
 * - 租户 ID 创建后不可改。
 * - 状态仅编辑态可改：后端 `TenantCreateRequest` 不接收 `status`，新租户一律走库默认值。
 * - 到期时间已从界面下线：表单不下发 `expire_time`，后端 `exclude_unset` 语义下省略即保持原值。
 */
const TenantFormModal = forwardRef<TenantFormModalRef, { onSaved: () => void }>(function TenantFormModal(
  { onSaved },
  ref,
) {
  const { t } = useTranslation();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<TenantItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<TenantFormValues>();

  useImperativeHandle(ref, () => ({
    openCreate() {
      setEditing(null);
      form.setFieldsValue({
        id: '',
        name: '',
        description: '',
        status: true,
      });
      setModalOpen(true);
    },
    openEdit(item: TenantItem) {
      setEditing(item);
      form.setFieldsValue({
        id: item.id,
        name: item.name,
        description: item.description || '',
        status: item.status === 1,
      });
      setModalOpen(true);
    },
  }));

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      // 租户 ID 与名称去除首尾空格后提交
      const tenantName = (values.name ?? '').trim();
      setSubmitting(true);
      if (editing) {
        // 不下发 plan_type / expire_time：两者已从界面下线，省略即保持库里原值（后端 exclude_unset 语义）
        const payload: TenantUpdatePayload = {
          name: tenantName,
          description: values.description,
          status: values.status ? 1 : 0,
        };
        await updateTenant(editing.id, payload);
        message.success(t('tenantManagement.updated'));
      } else {
        // 不下发 plan_type：后端 TenantCreateRequest 对该字段有默认值 free
        const payload: TenantCreatePayload = {
          id: (values.id ?? '').trim(),
          name: tenantName,
          description: values.description,
        };
        await createTenant(payload);
        message.success(t('tenantManagement.created'));
      }
      setModalOpen(false);
      onSaved();
    } catch (e: unknown) {
      if (e && typeof e === 'object' && 'errorFields' in e) return;
      message.error(e instanceof Error ? e.message : t('tenantManagement.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={editing ? t('tenantManagement.editTenant') : t('tenantManagement.createTenant')}
      open={modalOpen}
      onCancel={() => setModalOpen(false)}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      width={480}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        {!editing && (
          <Form.Item
            name="id"
            label={t('tenantManagement.tenantId')}
            rules={[{ required: true, whitespace: true, message: t('tenantManagement.requiredTenantId') }]}
            getValueFromEvent={(e) => e.target.value.replace(/\s+/g, '')}
          >
            <Input placeholder={t('tenantManagement.placeholderTenantId')} />
          </Form.Item>
        )}
        <Form.Item
          name="name"
          label={t('tenantManagement.name')}
          rules={[{ required: true, whitespace: true, message: t('tenantManagement.requiredName') }]}
          getValueFromEvent={(e) => e.target.value.replace(/\s+/g, '')}
        >
          <Input placeholder={t('tenantManagement.placeholderName')} />
        </Form.Item>
        <Form.Item name="description" label={t('tenantManagement.description')}>
          <Input placeholder={t('tenantManagement.placeholderDescription')} />
        </Form.Item>
        {editing && (
          <Form.Item
            name="status"
            label={t('common.status')}
            valuePropName="checked"
            extra={<Text type="secondary">{t('tenantManagement.statusHint')}</Text>}
          >
            <Switch />
          </Form.Item>
        )}
      </Form>
    </Modal>
  );
});

export default TenantFormModal;
