import i18n, { type i18n as I18nInstance, type InitOptions } from 'i18next';
import { initReactI18next } from 'react-i18next';
import zhResources from './locales/zh.json';
import enResources from './locales/en.json';

export const SUPPORTED_LOCALES = ['zh', 'en'] as const;

// 语言切换下拉的选项表（label/value 展示文案，跨壳恒定）随布局引擎收进
// @jonex/platform-theme 的 AppLayout 组件内，此处不再维护——
// 避免平台纯 UI 包被拽进 i18n 运行时。
export const LANGUAGE_STORAGE_KEY = 'jonex_locale';

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

export function normalizeLocale(value?: string | null): SupportedLocale {
  return SUPPORTED_LOCALES.includes(value as SupportedLocale) ? (value as SupportedLocale) : 'en';
}

/** 切语言唯一属主：更新 i18n 实例 + 持久化偏好 + 同窗 CustomEvent（hosted 子应用联动）+ iframe postMessage（standalone 兜底）。
 *  四个布局壳的 onSwitch 统一收敛到此处，避免广播语义在多处漂移；同语言早退避免无效切换。 */
export function switchLocale(i18n: I18nInstance, locale: string): void {
  if (locale === i18n.language) return;
  i18n.changeLanguage(locale);
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(LANGUAGE_STORAGE_KEY, locale);
  window.dispatchEvent(new CustomEvent('jonex:locale-change', { detail: locale }));
  window.document.querySelectorAll('iframe').forEach((iframe) => {
    iframe.contentWindow?.postMessage({ type: 'jonex:locale-change', locale }, '*');
  });
}

type TranslationRecord = Record<string, unknown>;
type ResourceMap = Record<string, Record<string, TranslationRecord>>;

function isPlainObject(value: unknown): value is TranslationRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function deepMergeTranslations(base: TranslationRecord, override: TranslationRecord): TranslationRecord {
  const merged: TranslationRecord = { ...base };

  for (const [key, overrideValue] of Object.entries(override)) {
    const baseValue = merged[key];
    merged[key] =
      isPlainObject(baseValue) && isPlainObject(overrideValue)
        ? deepMergeTranslations(baseValue, overrideValue)
        : overrideValue;
  }

  return merged;
}

export function createI18nInstance(options?: { resources?: ResourceMap; lng?: string }): I18nInstance {
  const instance = i18n.createInstance();
  const resources: ResourceMap = {
    zh: { translation: { ...zhResources } },
    en: { translation: { ...enResources } },
  };

  for (const lang of SUPPORTED_LOCALES) {
    const businessNamespaces = options?.resources?.[lang];
    if (!businessNamespaces) continue;

    for (const [namespace, businessResources] of Object.entries(businessNamespaces)) {
      const commonResources = resources[lang][namespace] ?? {};
      resources[lang][namespace] = deepMergeTranslations(commonResources, businessResources);
    }
  }

  const initOptions: InitOptions = {
    fallbackLng: 'en',
    lng: normalizeLocale(
      options?.lng ?? (typeof window !== 'undefined' ? window.localStorage.getItem(LANGUAGE_STORAGE_KEY) : null),
    ),
    interpolation: {
      escapeValue: false,
    },
    resources,
  };

  const syncDocumentLanguage = (locale: string) => {
    if (typeof document === 'undefined') return;
    document.documentElement.lang = normalizeLocale(locale) === 'zh' ? 'zh-CN' : 'en';
  };

  instance.on('languageChanged', syncDocumentLanguage);
  void instance
    .use(initReactI18next)
    .init(initOptions)
    .then(() => {
      syncDocumentLanguage(instance.language);
    });
  return instance;
}
