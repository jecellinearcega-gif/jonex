import type { TenantItem } from '../api/tenants';

type Translate = (key: string) => string;

/**
 * 内置租户的名称 / 描述走 i18n，其余租户展示库里的原值。
 *
 * 判定基准是**后端播种数据**（`deploy/postgres/migrations/006_seed_data.sql`）里的字面值：
 * 只有名称与描述都还是播种时那一份，才说明管理员没改过它，此时套用 i18n 文案；
 * 一旦改过就展示真实值。基准必须是播种值而不是这里的文案，否则条件永远不成立。
 */
const BUILT_IN_TENANTS: Record<string, { key: string; seedName: string; seedDescription: string }> = {
  tenant_jonex_demo: {
    key: 'demo',
    seedName: 'Jonex默认租户',
    seedDescription: '默认租户',
  },
  tenant_jonex_alpha: {
    key: 'alpha',
    seedName: 'Jonex Alpha 租户',
    seedDescription: '用于多租户登录选择流程',
  },
  tenant_jonex_beta: {
    key: 'beta',
    seedName: 'Jonex Beta 租户',
    seedDescription: '用于多租户登录选择流程',
  },
};

export function tenantDisplay(tenant: Pick<TenantItem, 'id' | 'name' | 'description'>, t: Translate) {
  const builtIn = BUILT_IN_TENANTS[tenant.id];
  const untouched =
    !!builtIn && tenant.name === builtIn.seedName && (tenant.description ?? '') === builtIn.seedDescription;

  return untouched
    ? {
        name: t(`tenantManagement.builtInTenants.${builtIn.key}.name`),
        description: t(`tenantManagement.builtInTenants.${builtIn.key}.description`),
      }
    : { name: tenant.name, description: tenant.description || '' };
}
