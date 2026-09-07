// Sažima fotografije koje klijent uploaduje kroz /admin (Decap CMS ih čuva
// sirove, bez kompresije). Pokreće se automatski pre svakog builda
// (vidi "prebuild" u package.json), a može i ručno: `npm run optimize-images`.
import { readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import sharp from 'sharp';

const TARGET_DIR = path.resolve('public/images/posts');
const MAX_WIDTH = 1920;
const SIZE_THRESHOLD_BYTES = 300 * 1024; // fajlove ispod ovoga ne diramo
const JPEG_QUALITY = 78;
const WEBP_QUALITY = 78;

const HANDLED_EXT = new Set(['.jpg', '.jpeg', '.png', '.webp']);

async function optimizeFile(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  if (!HANDLED_EXT.has(ext)) return;

  const original = await stat(filePath);
  const image = sharp(filePath);
  const metadata = await image.metadata();

  const needsResize = (metadata.width ?? 0) > MAX_WIDTH;
  const needsCompression = original.size > SIZE_THRESHOLD_BYTES;
  if (!needsResize && !needsCompression) return;

  let pipeline = sharp(filePath);
  if (needsResize) {
    pipeline = pipeline.resize({ width: MAX_WIDTH, withoutEnlargement: true });
  }

  if (ext === '.jpg' || ext === '.jpeg') {
    pipeline = pipeline.jpeg({ quality: JPEG_QUALITY, mozjpeg: true });
  } else if (ext === '.png') {
    pipeline = pipeline.png({ compressionLevel: 9 });
  } else if (ext === '.webp') {
    pipeline = pipeline.webp({ quality: WEBP_QUALITY });
  }

  const optimized = await pipeline.toBuffer();
  if (optimized.length >= original.size) return;

  await sharp(optimized).toFile(filePath);
  const savedKb = ((original.size - optimized.length) / 1024).toFixed(0);
  console.log(`  ✓ ${path.basename(filePath)}: ${(original.size / 1024).toFixed(0)}KB → ${(optimized.length / 1024).toFixed(0)}KB (ušteda ${savedKb}KB)`);
}

async function run() {
  let entries;
  try {
    entries = await readdir(TARGET_DIR);
  } catch {
    console.log(`Nema foldera ${TARGET_DIR}, preskačem optimizaciju slika.`);
    return;
  }

  console.log(`Optimizujem slike u ${TARGET_DIR}...`);
  for (const entry of entries) {
    const filePath = path.join(TARGET_DIR, entry);
    const info = await stat(filePath);
    if (info.isFile()) {
      await optimizeFile(filePath).catch((err) => {
        console.warn(`  ! Preskačem ${entry}: ${err.message}`);
      });
    }
  }
  console.log('Gotovo.');
}

run();
