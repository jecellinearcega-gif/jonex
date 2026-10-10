import { Select, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { FILTER_PHASES, PHASE_DISPLAY, type DocPhase } from '@/utils/docPhase';

export interface DocumentStatusFilterProps {
  /** 当前选中的 phase 列表；空数组表示「全部」。 */
  value: DocPhase[];
  onChange: (phases: DocPhase[]) => void;
}

/**
 * 线性状态筛选器（多选下拉）。选中的 phase 直接作为后端 `phase` 参数（多值 OR）。
 * 不选任何项即「全部」，由 placeholder 承载——不给「全部」单独占一个选项位，
 * 省掉它与具体状态之间的互斥处理。与 utils/docPhase 同源。
 */
export default function DocumentStatusFilter({ value, onChange }: DocumentStatusFilterProps) {
  const { t } = useTranslation();

  return (
    <Select<DocPhase[]>
      mode="multiple"
      value={value}
      onChange={onChange}
      placeholder={t('common.all')}
      style={{ minWidth: 200, maxWidth: 380 }}
      // 固定折叠阈值，不用 responsive：后者按容器宽度反推可容纳数量，
      // 在 200px 起步的宽度下会算出 0，明明只选两项也显示「+ 0 ...」
      maxTagCount={3}
      allowClear
      options={FILTER_PHASES.map((phase) => ({
        value: phase,
        label: t(PHASE_DISPLAY[phase].label),
      }))}
      // 选中项沿用各 phase 主题色，与列表里的状态徽章同一套颜色语言。
      // 不走 Tag 的 color prop：v6 会把自定义色当文字色配灰底，出来是灰底灰字；
      // 直接给 style 才能得到实色底 + 白字。
      tagRender={({ label, value: tagValue, closable, onClose }) => {
        const color = PHASE_DISPLAY[tagValue as DocPhase]?.color;
        return (
          <Tag
            closable={closable}
            onClose={onClose}
            style={{ background: color, color: '#fff', borderColor: color, marginInlineEnd: 4 }}
          >
            {label}
          </Tag>
        );
      }}
    />
  );
}
