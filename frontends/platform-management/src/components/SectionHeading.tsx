import { Flex, Typography } from 'antd';

const { Title } = Typography;

/**
 * 配置抽屉的分区头：语义图标 + 组标题。
 *
 * 抽屉的「基础配置 / 高级配置」两级共用同一视觉，避免各抽屉各写一份内联 Flex；
 * 图标颜色由调用方传主题 token（如 `token.colorPrimary`），不写死品牌色。
 */
export default function SectionHeading({
  icon,
  color,
  children,
}: {
  icon: React.ReactNode;
  color: string;
  children: React.ReactNode;
}) {
  return (
    <Flex align="center" gap={8}>
      <span style={{ color, fontSize: 15, display: 'inline-flex' }}>{icon}</span>
      <Title level={5} style={{ margin: 0 }}>
        {children}
      </Title>
    </Flex>
  );
}
