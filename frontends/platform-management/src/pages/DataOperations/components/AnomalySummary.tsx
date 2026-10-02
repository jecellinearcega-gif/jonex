import { Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DoAnomalyAsset, DoAnomalySummary, DoStage } from '../types';

interface Props {
  summary: DoAnomalySummary | null;
  /** 只取最近一条做摘要，不在卡内铺明细列表 */
  latest: DoAnomalyAsset | null;
  loading?: boolean;
}

const STAGE_ORDER: DoStage[] = ['parse', 'extract', 'compile'];

/** 失败数对应的汇总文案 key */
const STAGE_SUMMARY_KEY: Record<DoStage, string> = {
  parse: 'dataOperations.anomaly.parseFailed',
  extract: 'dataOperations.anomaly.extractFailed',
  compile: 'dataOperations.anomaly.compileFailed',
};

/** 异常资产的汇总：总数 + 分阶段失败数 + 最近一条。明细列表不在这里渲染。 */
export default function AnomalySummary({ summary, latest, loading }: Props) {
  const { t } = useTranslation();

  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;

  const countOf = (stage: DoStage) => summary?.byStage.find((item) => item.stage === stage)?.count ?? 0;

  return (
    <>
      <div className="yx-do-anomaly-summary">
        <div className="yx-do-anomaly-total">
          <span className="yx-do-anomaly-total-value">{(summary?.total ?? 0).toLocaleString()}</span>
          <span className="yx-do-anomaly-total-label">{t('dataOperations.anomaly.failedCount')}</span>
        </div>
        <div className="yx-do-anomaly-breakdown">
          {STAGE_ORDER.map((stage) => (
            <span key={stage} className="yx-do-anomaly-chip">
              {t(STAGE_SUMMARY_KEY[stage])}
              <em>{countOf(stage)}</em>
            </span>
          ))}
        </div>
      </div>

      {latest && (
        <div className="yx-do-anomaly-latest">
          <div className="yx-do-anomaly-latest-head">
            <span className="yx-do-anomaly-latest-label">{t('dataOperations.anomaly.latest')}</span>
            <strong>{latest.assetName}</strong>
            <span className="yx-do-anomaly-latest-stage">{t(`dataOperations.stage.${latest.stage}`)}</span>
            <span className="yx-do-anomaly-latest-time">{latest.occurredAt}</span>
          </div>
          <p className="yx-do-anomaly-latest-reason">{latest.reason}</p>
        </div>
      )}
    </>
  );
}
