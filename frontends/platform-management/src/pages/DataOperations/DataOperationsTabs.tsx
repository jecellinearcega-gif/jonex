import type { ReactNode } from 'react';
import { Tabs } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { DO_TAB_KEYS, type DoTabKey } from './utils';

interface Props {
  activeKey: DoTabKey;
  /** 三个分区的节点，key 与 tab 一一对应 */
  panes: Record<DoTabKey, ReactNode>;
}

/** 数据运营的页内 tab 栏。tab 落在 ?tab= 上，链接可直达、刷新不回到第一页。 */
export default function DataOperationsTabs({ activeKey, panes }: Props) {
  const [, setSearchParams] = useSearchParams();
  const { t } = useTranslation();

  return (
    <Tabs
      activeKey={activeKey}
      onChange={(key) => setSearchParams({ tab: key })}
      // 切走即卸载：三个分区都在挂载时取数，留着不卸会让切回来的数字停在几分钟前
      destroyOnHidden
      items={DO_TAB_KEYS.map((key) => ({
        key,
        label: t(`dataOperations.tabs.${key}`),
        // 分区根节点是 Fragment，靠容器的纵向 gap 排卡片；pane 自身不是 flex 容器，故补一层
        children: <div className="yx-do-tab-pane">{panes[key]}</div>,
      }))}
    />
  );
}
