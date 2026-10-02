import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Checkbox, Spin, message } from 'antd';
import { getRolePermissions, setRolePermissions, type RoleItem, type PermissionItem } from '../../api/roles';

export interface PermissionEditModalRef {
  open: (role: RoleItem) => void;
}

interface Props {
  perms: PermissionItem[];
  onSaved: () => void;
}

function permissionName(permission: PermissionItem, t: (key: string) => string) {
  const key = permission.code.replace(/:/g, '.');
  const translated = t(`rolePermission.builtInPermissions.${key}`);
  return translated === `rolePermission.builtInPermissions.${key}` ? permission.name : translated;
}

type PermissionGroupKey = 'platform' | 'tenant' | 'business';

const PERMISSION_GROUP_ORDER: PermissionGroupKey[] = ['platform', 'tenant', 'business'];

const PLATFORM_CODES = new Set([
  'datasource:read', 'datasource:write',
  'engine:read', 'engine:write',
  'adapter:read', 'adapter:write',
  'model:read', 'model:write',
  // 数据运营：码本身是租户级（见 deploy/postgres/migrations/006_seed_data.sql），
  // 只按产品要求归到「平台管理」组展示。
  'analytics:read',
]);

const TENANT_ADMIN_CODES = new Set([
  'tenant:write',
  'user:read', 'user:write',
  'role:read', 'role:write',
  'tenant:admin',
  'space:write',
  'audit:read',
]);

function permissionGroupKey(permission: PermissionItem): PermissionGroupKey {
  if (permission.code.startsWith('platform:') || PLATFORM_CODES.has(permission.code)) {
    return 'platform';
  }
  if (TENANT_ADMIN_CODES.has(permission.code)) {
    return 'tenant';
  }
  // 兜底落「业务功能」：`task_center:*` 的码本身是租户级（不带 `platform:` 前缀），
  // 归到这里是按产品要求展示，不是在按码的层级归类。
  return 'business';
}

const PermissionEditModal = forwardRef<PermissionEditModalRef, Props>(({ perms, onSaved }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<RoleItem | null>(null);
  const [checked, setChecked] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);

  useImperativeHandle(ref, () => ({
    open: async (r: RoleItem) => {
      setRole(r);
      setOpen(true);
      setLoading(true);
      try {
        const ids = await getRolePermissions(r.id);
        setChecked(ids);
      } catch (err: any) {
        message.error(err?.message || t('rolePermission.loadPermsFailed'));
      } finally {
        setLoading(false);
      }
    },
  }));

  const handleSave = async () => {
    if (!role) return;
    try {
      await setRolePermissions(role.id, checked);
      message.success(t('rolePermission.permsUpdated'));
      setOpen(false);
      onSaved();
    } catch (err: any) {
      message.error(err?.message || t('rolePermission.saveFailed'));
    }
  };

  const handleCancel = () => {
    setOpen(false);
  };

  const grouped = PERMISSION_GROUP_ORDER.map((key) => ({
    key,
    items: perms.filter((p) => permissionGroupKey(p) === key),
  })).filter((group) => group.items.length > 0);

  return (
    <Modal
      title={t('rolePermission.editPermsTitle', { name: role?.name })}
      open={open}
      onCancel={handleCancel}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      width={600}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : (
        <>
          <div style={{ marginBottom: 12 }}>
            <Checkbox
              indeterminate={checked.length > 0 && checked.length < perms.length}
              checked={checked.length === perms.length}
              onChange={(e) => {
                setChecked(e.target.checked ? perms.map((p) => p.id) : []);
              }}
            >
              <strong>{t('common.selectAll')}</strong>
            </Checkbox>
          </div>
          <Checkbox.Group
            value={checked}
            onChange={(v) => setChecked(v as number[])}
            style={{ width: '100%', display: 'flex', flexDirection: 'column', gap: 8 }}
          >
            {grouped.map((group) => (
              <div key={group.key}>
                <div style={{ fontWeight: 600, margin: '8px 0 4px', color: '#475569', fontSize: 13 }}>
                  {t(`rolePermission.permissionGroups.${group.key}`)}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
                  {group.items.map((p) => (
                    <Checkbox key={p.id} value={p.id}>
                      <strong>{permissionName(p, t)}</strong>
                      <br />
                      <span style={{ color: '#94a3b8', fontSize: 11 }}>
                        {p.code}
                      </span>
                    </Checkbox>
                  ))}
                </div>
              </div>
            ))}
          </Checkbox.Group>
        </>
      )}
    </Modal>
  );
});

PermissionEditModal.displayName = 'PermissionEditModal';
export default PermissionEditModal;
