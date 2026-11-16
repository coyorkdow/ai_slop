// @ts-check
import path from 'node:path';
import { defineConfig } from 'astro/config';
import { loadEnv, searchForWorkspaceRoot } from 'vite';
import { photosPlugin } from './photos-plugin.mjs';

// 照片文件夹在 .env 的 PHOTOS_DIR 里设置，多个文件夹用分号隔开，例如
// PHOTOS_DIR=C:\Users\me\Pictures\Gallery;D:\Photos\Travel
const env = loadEnv(process.env.NODE_ENV ?? 'development', process.cwd(), '');
const photosDirs = (env.PHOTOS_DIR || 'photos')
  .split(';')
  .map((d) => d.trim())
  .filter(Boolean)
  .map((d) => path.resolve(d));

export default defineConfig({
  // 部署到自定义域名后改成你的地址，例如 'https://photos.example.com'
  site: 'https://youtao-photo-gallery.pages.dev',
  // 中文页面在根目录 /，英文在 /en/。语言列表要和 src/i18n/index.ts 保持一致
  i18n: {
    locales: ['zh', 'en'],
    defaultLocale: 'zh',
    routing: { prefixDefaultLocale: false },
  },
  // 开发工具栏固定在页面底部中间，会挡住灯箱的底部栏
  devToolbar: { enabled: false },
  image: {
    // 原图很大，放宽 sharp 的像素上限，避免超大图片报错
    service: { entrypoint: 'astro/assets/services/sharp', config: { limitInputPixels: false } },
  },
  vite: {
    plugins: [photosPlugin(photosDirs)],
    // 允许开发服务器读取项目外的照片文件夹
    server: { fs: { allow: [searchForWorkspaceRoot(process.cwd()), ...photosDirs] } },
  },
});
