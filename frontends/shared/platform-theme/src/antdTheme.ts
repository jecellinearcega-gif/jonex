import type { ThemeConfig } from 'antd';

/**
 * 全局 antd 主题定制入口（ConfigProvider 的 theme）。
 *
 * 当前刻意留空：平台已回归 antd 默认外观，原先这里的 token / components 定制
 * 和 layout.css 里的 .ant-* 全局覆盖都已移除。
 *
 * 后续需要统一调整组件样式时，在这里集中配置 token / components —— 四个子应用
 * 与 _template 的 ConfigProvider 都已接线到本对象，改这一处即全量生效。
 * 不要在页面样式里新增 .ant-* 全局覆盖。
 */
export const antdTheme: ThemeConfig = {};
