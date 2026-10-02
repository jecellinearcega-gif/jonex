import React from 'react';
import { Tooltip } from 'antd';

interface DisabledTipProps {
  /** 禁用原因；为空时原样透出 children（不做任何包裹） */
  tip?: string;
  children: React.ReactNode;
}

/**
 * 「业务条件不满足」的禁用提示壳 —— 例如数据源未配置连接时不能立即同步。
 *
 * ⚠️ 权限类禁用请用 `@jonex/shared-lib` 的 `PermButton`，不要用这里：
 * 权限判断与无权限文案由它统一承载（同为 shell 下发的 permissions 口径）。
 * 两者叠加时给 `PermButton` 传 `noPermissionTip={false}`，由本组件出更具体的业务原因，
 * 否则会出现内外两层 Tooltip。
 *
 * 被禁用的元素不派发鼠标事件，Tooltip 直接挂在它上面不会触发，
 * 所以必须裹一层未禁用的 `inline-block` 元素承接 hover。
 */
export default function DisabledTip({ tip, children }: DisabledTipProps) {
  if (!tip) return <>{children}</>;
  return (
    <Tooltip title={tip}>
      <span style={{ display: 'inline-block', cursor: 'not-allowed' }}>{children}</span>
    </Tooltip>
  );
}
