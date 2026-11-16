declare module 'virtual:photos' {
  import type { ImageMetadata } from 'astro';
  export const photos: {
    file: string;
    /** 照片所在文件夹的绝对路径 */
    dir: string;
    image: ImageMetadata;
    /** 该文件夹 photos.json 里这张照片的条目 */
    meta: { title?: string; description?: string; hidden?: boolean };
  }[];
}
