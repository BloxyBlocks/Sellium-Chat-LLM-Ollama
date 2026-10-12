import { execFile } from 'node:child_process';
import { cp } from 'node:fs/promises';
import { resolve } from 'node:path';
import { promisify } from 'node:util';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, 'dist');
const run = promisify(execFile);

// Vite resuelve los módulos de Three/GSAP y las importaciones del modelo 3D.
// Copiar sólo index.html dejaba la aplicación Tauri sin esos módulos. Se
// invoca el binario de Node directamente para funcionar también con la
// política de PowerShell que bloquea npm.ps1 en Windows.
await run(process.execPath, [resolve(root, 'node_modules', 'vite', 'bin', 'vite.js'), 'build'], {
  cwd: root,
});
await cp(resolve(root, 'Assets'), resolve(dist, 'Assets'), { recursive: true, force: true });
console.log('Frontend estático preparado en dist/.');
