import { open } from 'node:fs/promises';
import path from 'node:path';
import exifr from 'exifr';
import type { ImageMetadata } from 'astro';
// 照片来自 .env 里 PHOTOS_DIR 指定的一个或多个文件夹，见 photos-plugin.mjs
import { photos as photoFiles } from 'virtual:photos';
import { DEFAULT_LANG, type Lang } from '../i18n';

interface PhotoText {
  title?: string;
  description?: string;
}

export interface Photo {
  file: string;
  image: ImageMetadata;
  /** 默认语言（中文）的标题和说明 */
  title?: string;
  description?: string;
  /** 其他语言的标题和说明，来自 photos.json 里对应语言的字段，例如 "en": { "title": ... } */
  translations: Partial<Record<Lang, PhotoText>>;
  date?: Date;
  exif: {
    camera?: string;
    lens?: string;
    focalLength?: string;
    aperture?: string;
    shutter?: string;
    iso?: string;
  };
}

function formatShutter(seconds?: number) {
  if (!seconds) return undefined;
  if (seconds >= 1) return `${seconds}s`;
  return `1/${Math.round(1 / seconds)}s`;
}

function formatCamera(make?: string, model?: string) {
  if (!model) return make;
  if (!make) return model;
  // 很多相机的 Model 已经包含品牌名，例如 "Canon EOS R5"
  const brand = make.split(' ')[0];
  return model.toLowerCase().startsWith(brand.toLowerCase()) ? model : `${brand} ${model}`;
}

// EXIF 位于文件开头（APP1 段最大 64KB），只读前 256KB 就够了，不必读整张 10MB 的原图。
// 自己打开并关闭文件句柄：exifr 自带的文件读取在新版 Node 下会泄漏句柄而报错。
async function readHead(filePath: string, bytes = 256 * 1024) {
  const handle = await open(filePath, 'r');
  try {
    const buffer = Buffer.alloc(bytes);
    const { bytesRead } = await handle.read(buffer, 0, bytes, 0);
    return buffer.subarray(0, bytesRead);
  } finally {
    await handle.close();
  }
}

async function readExif(filePath: string) {
  try {
    return await exifr.parse(await readHead(filePath), {
      pick: ['Make', 'Model', 'LensModel', 'FocalLength', 'FNumber', 'ExposureTime', 'ISO', 'DateTimeOriginal'],
    });
  } catch {
    return undefined;
  }
}

let cache: Promise<Photo[]> | undefined;

export function getPhotos(): Promise<Photo[]> {
  cache ??= loadPhotos();
  return cache;
}

async function loadPhotos(): Promise<Photo[]> {
  const photos = await Promise.all(
    photoFiles.map(async ({ file, dir, image, meta: info }) => {
      if (info.hidden) return undefined;
      const exif = await readExif(path.join(dir, file));
      const photo: Photo = {
        file,
        image,
        title: info.title,
        description: info.description,
        translations: { en: info.en },
        date: exif?.DateTimeOriginal instanceof Date ? exif.DateTimeOriginal : undefined,
        exif: {
          camera: formatCamera(exif?.Make, exif?.Model),
          lens: exif?.LensModel,
          focalLength: exif?.FocalLength ? `${Math.round(exif.FocalLength)}mm` : undefined,
          aperture: exif?.FNumber ? `ƒ/${exif.FNumber}` : undefined,
          shutter: formatShutter(exif?.ExposureTime),
          iso: exif?.ISO ? `ISO ${exif.ISO}` : undefined,
        },
      };
      return photo;
    }),
  );

  // 按拍摄时间倒序，没有 EXIF 日期的排在最后
  return photos
    .filter((p): p is Photo => p !== undefined)
    .sort((a, b) => (b.date?.getTime() ?? 0) - (a.date?.getTime() ?? 0));
}

/** 某种语言下的标题和说明，没有翻译的字段回退到默认语言 */
export function getPhotoText(photo: Photo, lang: Lang): PhotoText {
  const tr = lang === DEFAULT_LANG ? undefined : photo.translations[lang];
  return {
    title: tr?.title ?? photo.title,
    description: tr?.description ?? photo.description,
  };
}

export function formatDate(date?: Date) {
  if (!date) return undefined;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
