import type { ReactNode } from 'react';
import { Skeleton } from 'antd';
import {
  ApartmentOutlined,
  AppstoreOutlined,
  BookOutlined,
  BulbOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  LineChartOutlined,
  LinkOutlined,
  NodeIndexOutlined,
  PercentageOutlined,
  SearchOutlined,
  TeamOutlined,
} from '@ant-design/icons';

export interface StatCardItem {
  key: string;
  /** 已翻译的标签 */
  label: string;
  /** 已格式化的展示值（千分位由数据层保证） */
  value: string;
  /** 值下方的小徽标，如「近 7 天新增 +12,340」 */
  delta?: ReactNode;
  /** 图标底色 */
  accent: string;
  /** 图标标识，映射见下方 ICONS */
  iconKey: string;
}

const ICONS: Record<string, ReactNode> = {
  database: <DatabaseOutlined />,
  source: <CloudServerOutlined />,
  link: <LinkOutlined />,
  percent: <PercentageOutlined />,
  object: <AppstoreOutlined />,
  relation: <ApartmentOutlined />,
  entity: <NodeIndexOutlined />,
  concept: <BulbOutlined />,
  search: <SearchOutlined />,
  kb: <BookOutlined />,
  user: <TeamOutlined />,
  avg: <LineChartOutlined />,
};

interface Props {
  items: StatCardItem[];
  loading?: boolean;
  /** 骨架屏占位卡数，与真实卡数保持一致避免布局跳动 */
  skeletonCount?: number;
}

export default function StatCards({ items, loading, skeletonCount = 4 }: Props) {
  if (loading) {
    return (
      <div className="yx-do-kpi-grid">
        {Array.from({ length: skeletonCount }, (_, i) => (
          <div className="yx-card yx-do-kpi" key={i}>
            <Skeleton active paragraph={{ rows: 2 }} title={false} />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="yx-do-kpi-grid">
      {items.map((item) => (
        <div className="yx-card yx-do-kpi" key={item.key}>
          <span className="yx-do-kpi-icon" style={{ color: item.accent, background: `${item.accent}14` }}>
            {ICONS[item.iconKey]}
          </span>
          <div className="yx-do-kpi-main">
            <div className="yx-do-kpi-label">{item.label}</div>
            <div className="yx-do-kpi-value-row">
              <span className="yx-do-kpi-value">{item.value}</span>
              {item.delta && <span className="yx-do-kpi-delta">{item.delta}</span>}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
