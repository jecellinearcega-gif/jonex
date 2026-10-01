import React from 'react';
import { Card } from 'antd';

export interface PageContentProps {
  /** 页面主体内容。 */
  children?: React.ReactNode;
  /** 是否套白色卡片外壳，默认 true。false 走裸容器：无白底/边框/圆角/内边距，只保留撑满与滚动。 */
  showCard?: boolean;
  /** 覆盖默认样式，与对应模式的默认样式浅合并。 */
  style?: React.CSSProperties;
}

/**
 * 需要父级是 flex column（.yx-content 已提供）才能吃掉剩余高度；
 * minHeight 解除 flex item 的 auto 下限，内容超出时才能收缩而非撑破父级。
 */
const LAYOUT_STYLE: React.CSSProperties = {
  flex: '1 1 auto',
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column',
};

/** 圆角跟主题走，不自己定值——否则调 --yx-card-radius 时这张主体卡片会掉队。 */
const CARD_STYLE: React.CSSProperties = {
  ...LAYOUT_STYLE,
  borderRadius: 'var(--yx-card-radius)',
};

/** 卡片模式下滚动由 .ant-card-body 承担，裸容器没有那一层，改由自身滚。 */
const PLAIN_STYLE: React.CSSProperties = {
  ...LAYOUT_STYLE,
  overflow: 'auto',
};

/** 撑满卡片并自行滚动，内容不会溢出卡片边框。 */
const BODY_STYLE: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflow: 'auto',
};

/** 页面主体：默认是不带标题的 Card，铺满页头之下的剩余高度。 */
const PageContent: React.FC<PageContentProps> = ({ children, showCard = true, style }) => {
  if (!showCard) {
    return <div style={{ ...PLAIN_STYLE, ...style }}>{children}</div>;
  }
  return (
    <Card style={{ ...CARD_STYLE, ...style }} styles={{ body: BODY_STYLE }}>
      {children}
    </Card>
  );
};

export default PageContent;
