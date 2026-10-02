import { Empty, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DoProportionItem } from '../types';

/**
 * 默认色板，顺序与 DO_ASSET_COMPOSITION 的类型顺序一致，故无需页面显式指定颜色。
 * 末位 `other` 用中性灰，与 `AssetTypeTag` 的 `--other` 同一色 —— 色板少一个颜色时
 * 下标会取模回绕，第 7 行会拿到第 1 行的蓝，看上去像「其他」属于文档。
 */
const PALETTE = ['#3b82f6', '#10b981', '#8b5cf6', '#0ea5e9', '#f97316', '#ec4899', '#6b7280'];

interface Props {
  items: DoProportionItem[];
  loading?: boolean;
  /** 数值列展示的内容，默认显示 count 的千分位 */
  renderValue?: (item: DoProportionItem, percent: number) => React.ReactNode;
  /**
   * 覆盖「百分比」口径：返回值同时用于显示与条宽。
   * 不传则用 count 占同组总量的比例。检索方式的「参与率」走这里。
   */
  percentOf?: (item: DoProportionItem, share: number) => number;
  colorOf?: (item: DoProportionItem, index: number) => string;
}

/**
 * 横向占比条列表。
 * 不用 antd Progress：它自带的内外边距与截图里的紧凑列表不符。
 * 百分比一律由 count 与同组总量算出，不取常量里的写死值，保证与数字自洽。
 */
export default function ProportionList({ items, loading, renderValue, percentOf, colorOf }: Props) {
  const { t } = useTranslation();

  if (loading) return <Skeleton active paragraph={{ rows: 5 }} />;
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('dataOperations.empty.noData')} />;

  const total = items.reduce((sum, item) => sum + item.count, 0) || 1;

  return (
    <ul className="yx-do-proportion">
      {items.map((item, index) => {
        const share = (item.count / total) * 100;
        const percent = percentOf ? percentOf(item, share) : share;
        const color = colorOf ? colorOf(item, index) : PALETTE[index % PALETTE.length];

        return (
          <li key={item.key} className="yx-do-proportion-item">
            <div className="yx-do-proportion-head">
              <span className="yx-do-proportion-label">{item.labelKey ? t(item.labelKey) : item.label}</span>
              <span className="yx-do-proportion-value">
                {renderValue ? renderValue(item, percent) : item.count.toLocaleString()}
                <em className="yx-do-proportion-percent">{percent.toFixed(1)}%</em>
              </span>
            </div>
            <div className="yx-do-proportion-track">
              <span
                className="yx-do-proportion-bar"
                style={{ width: `${Math.min(percent, 100)}%`, background: color }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
