import { Layout } from 'antd';
import { Outlet } from 'react-router-dom';
import useDocumentTitle from '@/hooks/useDocumentTitle';

const { Content } = Layout;

/**
 * 页面容器。高度必须在这一层定死（min-height 撑出来的高度不被百分比解析，链会断），
 * flex column 给 PageContent 的 flex:1 提供父级上下文，overflow:auto 承接内容超出。
 */
export default function HostedLayout() {
  useDocumentTitle();
  return (
    <Content style={{ padding: 0, height: '100%', overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
      <Outlet />
    </Content>
  );
}
