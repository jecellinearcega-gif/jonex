import { createCache } from '@ant-design/cssinjs';

/**
 * 全局共享的 cssinjs cache（跨所有应用统一同一实例）。
 *
 * 通过 window 全局持有，即使各应用各自打包一份 @ant-design/cssinjs 模块，
 * cache 对象仍是同一个。配合 `<StyleProvider cache={sharedCache}>` 后：
 * 1. 所有 `<style>` 打上同一 instanceId（删除时校验一致，不跨实例误删）
 * 2. useGlobalCache 引用计数全局一致（主应用下拉短暂卸载时，页面内其它 popup
 *    组件仍持有引用，组件 token 变量标签 .antd.ant-dropdown-css-var 不会被误删）
 */
const globalObj = (typeof window !== 'undefined' ? window : globalThis) as {
  __JONEX_CSSINJS_CACHE__?: ReturnType<typeof createCache>;
};
globalObj.__JONEX_CSSINJS_CACHE__ = globalObj.__JONEX_CSSINJS_CACHE__ || createCache();
export const sharedCache: ReturnType<typeof createCache> = globalObj.__JONEX_CSSINJS_CACHE__;
