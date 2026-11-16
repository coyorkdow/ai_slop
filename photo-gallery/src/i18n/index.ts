// 界面文案在同目录的 <语言>.json 里。新增语言：加一个 json，在下面登记，
// 并同步修改 astro.config.mjs 里的 i18n.locales。
import zh from './zh.json';
import en from './en.json';

export type Messages = typeof zh;

export const LOCALES = {
  zh: { htmlLang: 'zh-CN', messages: zh },
  // 标注为 Messages：en.json 少了 zh.json 里的某个文案时，类型检查会报错
  en: { htmlLang: 'en', messages: en as Messages },
} as const;

export type Lang = keyof typeof LOCALES;

/** 默认语言的页面在网站根目录，其他语言在 /<语言>/ 下 */
export const DEFAULT_LANG: Lang = 'zh';
export const LANGS = Object.keys(LOCALES) as Lang[];

/** 把 Astro.currentLocale 转成 Lang，不认识的语言按默认语言处理 */
export function getLang(locale: string | undefined): Lang {
  return locale && locale in LOCALES ? (locale as Lang) : DEFAULT_LANG;
}

export function getMessages(lang: Lang): Messages {
  return LOCALES[lang].messages;
}
