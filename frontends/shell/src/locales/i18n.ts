import { createI18nInstance } from '@jonex/i18n-resources';
import { shellResources } from './shellResources';

// resources 与共享资源深合并（只覆盖同名 key），仅作用于壳自己的实例，不影响子应用
const i18n = createI18nInstance({ resources: shellResources });

export default i18n;
