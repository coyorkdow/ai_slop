// 从项目外的一个或多个照片文件夹读取照片，生成虚拟模块 `virtual:photos`：
// 每张照片都是一个静态 import，这样 Astro 能照常生成缩略图和大图。
// 每个文件夹可以有自己的 photos.json，按文件名（不区分大小写）给照片加标题和说明。
import fs from 'node:fs';
import path from 'node:path';

const VIRTUAL_ID = 'virtual:photos';
const RESOLVED_ID = '\0' + VIRTUAL_ID;
const IMAGE_RE = /\.(jpe?g|png|webp)$/i;
const META_FILE = 'photos.json';

function readMeta(dir) {
  const metaPath = path.join(dir, META_FILE);
  if (!fs.existsSync(metaPath)) return new Map();
  const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
  return new Map(Object.entries(meta).map(([file, info]) => [file.toLowerCase(), info]));
}

export function photosPlugin(dirs) {
  const absDirs = dirs.map((d) => path.resolve(d));
  const toPosix = (p) => p.split(path.sep).join('/');

  return {
    name: 'photos-dir',

    resolveId(id) {
      if (id === VIRTUAL_ID) return RESOLVED_ID;
    },

    load(id) {
      if (id !== RESOLVED_ID) return;

      const imports = [];
      const entries = [];
      for (const dir of absDirs) {
        if (!fs.existsSync(dir)) {
          throw new Error(`照片文件夹不存在：${dir}。请检查 .env 里的 PHOTOS_DIR。`);
        }
        const meta = readMeta(dir);
        for (const file of fs.readdirSync(dir).filter((f) => IMAGE_RE.test(f)).sort()) {
          const name = `p${imports.length}`;
          imports.push(`import ${name} from ${JSON.stringify(toPosix(path.join(dir, file)))};`);
          const info = meta.get(file.toLowerCase()) ?? {};
          entries.push(
            `{ file: ${JSON.stringify(file)}, dir: ${JSON.stringify(dir)}, image: ${name}, meta: ${JSON.stringify(info)} }`,
          );
        }
      }

      return [...imports, `export const photos = [\n  ${entries.join(',\n  ')}\n];`].join('\n');
    },

    // 开发模式：照片文件夹里增删改照片或 photos.json 时，重新生成模块并刷新页面
    configureServer(server) {
      for (const dir of absDirs) server.watcher.add(dir);

      const onChange = (file) => {
        const abs = path.resolve(file);
        if (!absDirs.includes(path.dirname(abs))) return;
        if (!IMAGE_RE.test(abs) && path.basename(abs) !== META_FILE) return;

        const graphs = server.environments
          ? Object.values(server.environments).map((env) => env.moduleGraph)
          : [server.moduleGraph];
        for (const graph of graphs) {
          // 连同引用它的模块（photos.ts、页面）一起失效
          const queue = [graph.getModuleById(RESOLVED_ID)];
          const seen = new Set();
          while (queue.length) {
            const mod = queue.pop();
            if (!mod || seen.has(mod)) continue;
            seen.add(mod);
            graph.invalidateModule(mod);
            queue.push(...mod.importers);
          }
        }
        server.ws.send({ type: 'full-reload' });
      };

      server.watcher.on('add', onChange);
      server.watcher.on('unlink', onChange);
      server.watcher.on('change', onChange);
    },
  };
}
