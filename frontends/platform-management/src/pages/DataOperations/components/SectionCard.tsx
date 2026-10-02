import type { ReactNode } from 'react';

interface Props {
  title: ReactNode;
  /** 标题右侧的灰色小字，如「按最终回答引用次数排序」 */
  hint?: ReactNode;
  /** 右上角操作区：筛选器、分段器等 */
  extra?: ReactNode;
  /** 标题下方的说明行 */
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** 模块内所有分区的统一外壳：白卡 + 标题行 + 内容 */
export default function SectionCard({ title, hint, extra, description, children, className }: Props) {
  return (
    <section className={`yx-card yx-do-card${className ? ` ${className}` : ''}`}>
      <header className="yx-do-card-head">
        <div className="yx-do-card-title-wrap">
          <h3 className="yx-do-card-title">{title}</h3>
          {hint && <span className="yx-do-card-hint">{hint}</span>}
        </div>
        {extra && <div className="yx-do-card-extra">{extra}</div>}
      </header>
      {description && <p className="yx-do-card-desc">{description}</p>}
      <div className="yx-do-card-body">{children}</div>
    </section>
  );
}
