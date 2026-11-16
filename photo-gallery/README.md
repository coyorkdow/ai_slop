# 照片网站

基于 Astro 的静态相册：等高行网格 + PhotoSwipe 灯箱 + 构建时读取 EXIF。

## 本地开发

```bash
npm install
npm run dev      # http://localhost:4321
npm run build    # 输出到 dist/
npm run preview  # 预览构建结果
```

## 照片文件夹

照片不放在项目里，而是从 `.env` 里 `PHOTOS_DIR` 指定的文件夹读取（参考 `.env.example`），多个文件夹用分号隔开：

```
PHOTOS_DIR=C:\Users\me\Pictures\Gallery;D:\Photos\Travel
```

1. 把照片放进这些文件夹（jpg / png / webp，只读第一层，不读子文件夹）。
2. 可选：在照片所在文件夹的 `photos.json` 里按文件名加 `title`、`description`，或用 `"hidden": true` 隐藏某张照片。每个文件夹各有自己的 `photos.json`，文件名不区分大小写。英文页面的标题和说明写在 `en` 字段里，没写时显示中文：

   ```json
   {
     "DSCF4028.jpg": {
       "title": "维多利亚港",
       "description": "香港。从太平山顶俯瞰中环与维港……",
       "en": { "title": "Victoria Harbour", "description": "Hong Kong. Central and the harbour seen from Victoria Peak…" }
     }
   }
   ```
3. 所有文件夹的照片合在一起，按 EXIF 拍摄时间倒序排列。

修改 `.env` 后需要重启 `npm run dev`。

`npm run dev` 运行时，在照片文件夹里增删照片或修改 `photos.json`，页面会自动刷新。

构建时每张照片会生成：
- 网格缩略图：400 / 800 / 1200px 宽的 WebP
- 灯箱大图：长边 2560px 的 WebP

**原图不会被发布**。第一次构建时处理大图比较慢，之后会使用 `node_modules/.astro` 里的缓存。

## 中英双语

中文页面在 `/`，英文页面在 `/en/`，右上角可以切换。界面文案（站点名称、描述、灯箱按钮提示等）在 `src/i18n/zh.json` 和 `src/i18n/en.json` 里修改，两个文件的字段要保持一致。照片的标题和说明见上面 `photos.json` 的 `en` 字段。

## 灯箱快捷键

| 按键 | 作用 |
|---|---|
| ← / → | 上一张 / 下一张 |
| F | 浏览器全屏（工具栏也有按钮；iPhone Safari 不支持网页全屏，不显示按钮） |
| I | 展开 / 收起底部说明栏 |
| Esc | 退出全屏或关闭灯箱 |

## 部署到 Cloudflare Pages

照片在本机的文件夹里，所以在本地构建后用 wrangler 上传（Cloudflare 的服务器读不到本机的照片，不能用"连接 Git 仓库自动构建"）：

```bash
npx wrangler login
npm run deploy
```

第一次运行时会创建名为 `youtao-photo-gallery` 的项目，访问地址为 `https://youtao-photo-gallery.pages.dev`。如果这个名字已被占用，需要修改 `package.json` 里 `deploy` 脚本的 `--project-name`。
