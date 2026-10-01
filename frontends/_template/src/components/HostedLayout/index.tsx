import { Layout } from 'antd';
import { Outlet } from 'react-router-dom';
import useDocumentTitle from '@/hooks/useDocumentTitle';

const { Content } = Layout;

/**
 * 承载子应用页面。页面根靠 height:100% / flex:1 铺满剩余高度时，只认父级「声明的 height」——
 * 用 min-height 撑出来的高度不被百分比解析，链会在这一层静默断掉（embed 挂载下表现为铺不满）。
 * 因此这里给确定高度，并用 overflow:auto 承接内容超出，避免溢出被截断。
 * flex column 是 PageContent(flex:1) 的父级上下文，缺了它页面骨架会静默失效。
 */
export default function HostedLayout() {
  useDocumentTitle();
  return (
    <Content style={{ padding: 0, height: '100%', overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
      <Outlet />
    </Content>
  );
}
