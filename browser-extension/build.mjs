import { build } from 'esbuild';
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = new URL('.', import.meta.url).pathname;
const source = resolve(root, 'src');
const output = resolve(root, 'dist');

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

await build({
  entryPoints: [resolve(source, 'background.ts')],
  outfile: resolve(output, 'background.js'),
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: ['chrome109'],
});

for (const name of ['content', 'options']) {
  await build({
    entryPoints: [resolve(source, `${name}.ts`)],
    outfile: resolve(output, `${name}.js`),
    bundle: true,
    format: 'iife',
    platform: 'browser',
    target: ['chrome109'],
  });
}

const manifest = JSON.parse(
  await readFile(resolve(source, 'manifest.json'), 'utf8')
);
const version = process.env['BLREC_EXTENSION_VERSION'];
if (version) {
  if (!/^\d+(?:\.\d+){0,3}$/.test(version)) {
    throw new Error(`invalid Chromium extension version: ${version}`);
  }
  manifest.version = version;
}
await writeFile(
  resolve(output, 'manifest.json'),
  `${JSON.stringify(manifest, null, 2)}\n`,
  'utf8'
);
await mkdir(resolve(output, 'icons'), { recursive: true });
await cp(
  resolve(root, '../webapp/src/assets/icons/icon-128x128.png'),
  resolve(output, 'icons', 'icon-128.png')
);
for (const name of ['options.html', 'options.css', 'content.css']) {
  await cp(resolve(source, name), resolve(output, name));
}
