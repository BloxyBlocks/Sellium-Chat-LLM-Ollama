import { cp, copyFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, 'dist');
await mkdir(dist, { recursive: true });
await copyFile(resolve(root, 'index.html'), resolve(dist, 'index.html'));
await cp(resolve(root, 'Assets'), resolve(dist, 'Assets'), { recursive: true, force: true });
console.log('Frontend estático preparado en dist/.');
