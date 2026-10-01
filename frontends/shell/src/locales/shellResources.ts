/**
 * shell 私有的 i18n 文案（模拟态到期提示）。
 *
 * 刻意**不进** `@jonex/i18n-resources` 的共享资源包：这两条只有壳用到，塞进共享包等于
 * 让所有子应用都背一份用不上的文案。经 `createI18nInstance({ resources })` 深合并进
 * 壳自己的实例（同 namespace 同 key 才覆盖，不新增 namespace）。
 */
export const shellResources = {
  zh: {
    translation: {
      impersonation: {
        expiring: '模拟会话即将结束，届时将自动退出模拟并回到原租户',
        countdown: '{{time}} 后自动退出模拟',
      },
    },
  },
  en: {
    translation: {
      impersonation: {
        expiring: 'The impersonation session is about to end and will exit automatically.',
        countdown: 'Exiting impersonation in {{time}}',
      },
    },
  },
};
