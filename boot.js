/* ==========================================================================
 * SELLIUM · Boot cinemático estilo JARVIS (v2 · minimalista)
 * --------------------------------------------------------------------------
 * Three.js (WebGL) + GSAP + EffectComposer/UnrealBloomPass. ES Module puro.
 *
 * CONCEPTO
 *   Fondo negro, líneas finas y luz. Todo se "dibuja" solo: un núcleo se
 *   enciende, una nube de partículas converge en espiral hasta formar una
 *   esfera, anillos HUD se trazan uno a uno, un radar barre el conjunto,
 *   cometas orbitan, el título se decodifica letra a letra y, al final, un
 *   pulso dorado (SYSTEM READY) recorre todo antes de atravesar la cámara.
 *
 * ARQUITECTURA
 *   createShell()    → ~2600 partículas: caos → esfera (vertex shader, 1 draw call)
 *   createDust()     → polvo ambiental con paralaje (y efecto "warp" al salir)
 *   createHud()      → anillos (trazado progresivo con drawRange), ticks, arcos,
 *                      giroscopio y cometas orbitales
 *   createSweep()    → radar con estela (shader)
 *   createShocks()   → ondas de choque (ignición y READY)
 *   createCoreGlow() → resplandor del núcleo
 *   createEngine()   → renderer + escena + bloom + bucle rAF
 *   buildMainTimeline() / buildFadeTimeline() → secuencia GSAP
 *   play()           → API pública (Promise)
 *
 * PRINCIPIO CLAVE (igual que antes): GSAP NO toca objetos de Three.js. Anima el
 * objeto `state`; el bucle rAF lo lee cada frame. Render y animación desacoplados.
 *
 * USO
 *   import { play } from './boot.js';
 *   await play({ duration: 6, waitFor: promesaOpcional, onFadeStart: () => {} });
 * ========================================================================== */

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { gsap } from 'gsap';

/* --------------------------------------------------------------------------
 * CONFIGURACIÓN (todo ajustable desde play({ config: { ... } }))
 * -------------------------------------------------------------------------- */
export const DEFAULTS = {
  selectors: {
    root: '.boot-fx',
    canvas: '#boot-canvas',
    hud: '.boot-hud',
    title: '.boot-title',
    status: '.boot-status',
    bar: '.boot-bar i',
    pct: '.boot-pct',
    mod: '.boot-mod',
    axis: '.boot-axis',
    scan: '.boot-scan',
    corners: '.boot-corner'          // (varios)
  },
  text: {
    init: 'INICIALIZANDO',
    load: 'CARGANDO MÓDULOS',
    sync: 'SINCRONIZANDO NÚCLEO',
    ready: 'SYSTEM READY'
  },
  modules: 12,                        // contador "MÓD 00/12" (decorativo)

  duration: 6,                        // s totales (la secuencia se escala a este valor)
  shellCount: 2600,                   // partículas de la esfera
  dustCount: 1100,                    // polvo ambiental
  colors: { cyan: 0x00d4ff, deep: 0x00a9d2, gold: 0xffc933 },
  intensity: 1.9,                     // brillo HDR de las partículas (alimenta el bloom)
  camera: { fov: 45, z: 40 },
  bloom: { start: 0.5, full: 1.3, end: 0.9, flash: 2.2, radius: 0.65, threshold: 0.02 }
};

/** Duración nominal: tiempos de la timeline expresados sobre estos 6 s. */
const NOMINAL_TOTAL = 6.0;
const MAIN_END = 5.4;               // línea principal 0 → 5.4 s
const FADE_LEN = 0.6;               // fade-out final (atraviesa la cámara)
const T_READY = 4.35;               // instante del pulso "SYSTEM READY"

/* --------------------------------------------------------------------------
 * UTILIDADES
 * -------------------------------------------------------------------------- */
function merge(base, extra) {
  if (!extra) return structuredClone(base);
  const out = Array.isArray(base) ? [...base] : { ...base };
  for (const k of Object.keys(extra)) {
    const v = extra[k];
    out[k] = v && typeof v === 'object' && !Array.isArray(v) && typeof base[k] === 'object'
      ? merge(base[k], v)
      : v;
  }
  return out;
}

const TAU = Math.PI * 2;
const hdr = (hex, k) => new THREE.Color(hex).multiplyScalar(k);

/** Estado animado por GSAP. Es lo ÚNICO que la timeline toca. */
function makeState(cfg) {
  return {
    form: 0, shell: 0, dust: 0,                              // partículas
    ring1: 0, ring2: 0, ring3: 0, ring4: 0, ring5: 0, gyro: 0, // trazado de anillos (0..1)
    spin: 0, nodes: 0, sweep: 0, core: 0,                    // giro, cometas, radar, núcleo
    shock1: 0, shock2: 0, pulse: 0, flash: 0, gold: 0,       // ondas y color
    bloom: cfg.bloom.start,
    camZ: cfg.camera.z + 9, roll: 0.1, warp: 0               // cámara
  };
}

/* ==========================================================================
 * SHADERS DE PUNTOS (comparten el fragment: disco suave con brillo)
 * ========================================================================== */
const POINT_FRAG = /* glsl */`
  uniform vec3  uColA;
  uniform vec3  uColB;
  uniform float uIntensity;
  varying float vA;
  varying float vG;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = pow(smoothstep(0.5, 0.0, d), 1.6);
    gl_FragColor = vec4(mix(uColA, uColB, vG) * uIntensity, a * vA);
  }
`;

/* Esfera: cada partícula viaja de aStart (caos) a position (objetivo en la esfera)
   describiendo una espiral. Retardo por partícula (aRand.x) ⇒ llegada escalonada. */
const SHELL_VERT = /* glsl */`
  attribute vec3 aStart;
  attribute vec4 aRand;      // x retardo, y tamaño, z dorado(0/1), w fase
  uniform float uForm, uTime, uScale, uSize, uOpacity, uGold, uPulse;
  varying float vA;
  varying float vG;
  void main() {
    float t = clamp((uForm - aRand.x * 0.4) / 0.6, 0.0, 1.0);
    float e = 1.0 - pow(1.0 - t, 3.0);

    vec3 s = aStart;
    float ang = (1.0 - e) * (2.0 + aRand.w * 3.0);
    float c = cos(ang), sn = sin(ang);
    s.xz = mat2(c, -sn, sn, c) * s.xz;                    // giro en espiral al converger

    vec3 p = mix(s, position, e);
    float breathe = 1.0 + 0.03 * sin(uTime * 1.6 + aRand.w * 6.2831853) + uPulse * 0.25;
    p *= mix(1.0, breathe, e);

    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(aRand.y * uSize * uScale / -mv.z * (0.6 + 0.4 * e), 1.0, 40.0);
    vA = uOpacity * (0.15 + 0.85 * e);
    vG = max(aRand.z, uGold);
  }
`;

/* Polvo: deriva lenta en Z con wrap; uWarp lo lanza hacia la cámara al salir. */
const DUST_VERT = /* glsl */`
  attribute vec4 aRand;      // x velocidad, y tamaño, z fase, w brillo
  uniform float uTime, uWarp, uScale, uSize, uOpacity, uGold;
  varying float vA;
  varying float vG;
  void main() {
    vec3 p = position;
    float zr = mod(p.z + 40.0 + uTime * aRand.x + uWarp, 70.0);
    p.z = zr - 40.0;
    p.xy += vec2(sin(uTime * 0.2 + aRand.z * 6.2831853), cos(uTime * 0.17 + aRand.z * 6.2831853)) * 0.6;

    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(aRand.y * uSize * uScale / -mv.z, 1.0, 24.0);
    float edge = smoothstep(0.0, 8.0, zr) * smoothstep(70.0, 55.0, zr);
    vA = uOpacity * aRand.w * edge;
    vG = uGold * 0.6;
  }
`;

/* Cometas orbitales: cabeza + estela (alpha decreciente por vértice). */
const DOT_VERT = /* glsl */`
  attribute vec4 aRand;      // y tamaño, z dorado, w alpha
  uniform float uScale, uSize, uOpacity, uGold;
  varying float vA;
  varying float vG;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(aRand.y * uSize * uScale / -mv.z, 1.0, 40.0);
    vA = aRand.w * uOpacity;
    vG = max(aRand.z, uGold);
  }
`;

/* ==========================================================================
 * 1) ESFERA DE PARTÍCULAS
 * ========================================================================== */
function createShell(cfg, U) {
  const N = cfg.shellCount, R = 3.6;
  const target = new Float32Array(N * 3);
  const start = new Float32Array(N * 3);
  const rnd = new Float32Array(N * 4);

  for (let i = 0; i < N; i++) {
    // Esfera de Fibonacci (reparto uniforme)
    const y = 1 - ((i + 0.5) / N) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = i * 2.399963;
    const rad = R * (1 + (Math.random() - 0.5) * 0.06);
    target.set([Math.cos(th) * r * rad, y * rad, Math.sin(th) * r * rad], i * 3);

    // Origen aleatorio en una cáscara lejana
    const u = Math.random() * 2 - 1, a = Math.random() * TAU, s = Math.sqrt(1 - u * u);
    const dist = 16 + Math.random() * 34;
    start.set([Math.cos(a) * s * dist, u * dist, Math.sin(a) * s * dist], i * 3);

    rnd.set([Math.random(), 0.55 + Math.random() * 1.1, Math.random() < 0.1 ? 1 : 0, Math.random()], i * 4);
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(target, 3));
  geo.setAttribute('aStart', new THREE.BufferAttribute(start, 3));
  geo.setAttribute('aRand', new THREE.BufferAttribute(rnd, 4));

  const u = {
    ...U,
    uForm: { value: 0 }, uOpacity: { value: 0 }, uPulse: { value: 0 }, uSize: { value: 0.11 }
  };
  const mat = new THREE.ShaderMaterial({
    vertexShader: SHELL_VERT, fragmentShader: POINT_FRAG, uniforms: u,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending
  });
  const obj = new THREE.Points(geo, mat);
  obj.frustumCulled = false;

  return {
    object: obj,
    update(dt, t, st) {
      u.uForm.value = st.form;
      u.uOpacity.value = st.shell;
      u.uPulse.value = st.pulse;
      obj.rotation.y += dt * (0.12 + 0.35 * st.spin);
      obj.rotation.x = Math.sin(t * 0.3) * 0.2;
    }
  };
}

/* ==========================================================================
 * 2) POLVO AMBIENTAL
 * ========================================================================== */
function createDust(cfg, U) {
  const N = cfg.dustCount;
  const pos = new Float32Array(N * 3);
  const rnd = new Float32Array(N * 4);
  for (let i = 0; i < N; i++) {
    pos.set([(Math.random() - 0.5) * 90, (Math.random() - 0.5) * 56, (Math.random() - 0.5) * 70], i * 3);
    rnd.set([0.6 + Math.random() * 1.6, 0.4 + Math.random() * 1.0, Math.random(), 0.25 + Math.random() * 0.75], i * 4);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('aRand', new THREE.BufferAttribute(rnd, 4));

  const u = { ...U, uWarp: { value: 0 }, uOpacity: { value: 0 }, uSize: { value: 0.14 } };
  const mat = new THREE.ShaderMaterial({
    vertexShader: DUST_VERT, fragmentShader: POINT_FRAG, uniforms: u,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending
  });
  const obj = new THREE.Points(geo, mat);
  obj.frustumCulled = false;

  return {
    object: obj,
    update(dt, t, st) {
      u.uWarp.value = st.warp * 90;
      u.uOpacity.value = st.dust * 0.75;
    }
  };
}

/* ==========================================================================
 * 3) HUD: anillos, ticks, arcos, giroscopio y cometas
 * --------------------------------------------------------------------------
 * Los trazos se "dibujan" con geometry.setDrawRange (progreso 0..1 en state).
 * Los materiales se tiñen hacia dorado con state.gold (peso distinto por pieza).
 * ========================================================================== */
function createHud(cfg, U) {
  const group = new THREE.Group();
  const draws = [];      // { obj, key, n, per, m, base }
  const tints = [];      // { m, a, b, w }

  const mat = (Cls, hex, k, opacity, goldW = 0, extra = {}) => {
    const m = new Cls({
      color: hdr(hex, k), transparent: true, opacity,
      depthWrite: false, blending: THREE.AdditiveBlending, ...extra
    });
    if (goldW) tints.push({ m, a: m.color.clone(), b: hdr(cfg.colors.gold, k), w: goldW });
    return m;
  };
  const reg = (obj, key, n, per, m, base) => {
    obj.visible = false;
    draws.push({ obj, key, n, per, m, base });
    return obj;
  };
  const geoFrom = arr => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(arr, 3));
    return g;
  };
  const circlePts = (r, seg) => {
    const a = new Float32Array((seg + 1) * 3);
    for (let i = 0; i <= seg; i++) {
      const t = (i / seg) * TAU;
      a.set([Math.cos(t) * r, Math.sin(t) * r, 0], i * 3);
    }
    return a;
  };
  const addLoop = (parent, r, seg, key, m, base) =>
    parent.add(reg(new THREE.Line(geoFrom(circlePts(r, seg)), m), key, seg + 1, 1, m, base));

  const cyan = cfg.colors.cyan, deep = cfg.colors.deep, gold = cfg.colors.gold;

  /* --- Anillo 1 (r 6.2): círculo continuo + 4 muescas cardinales (tiende a dorado) --- */
  const g1 = new THREE.Group();
  {
    const m = mat(THREE.LineBasicMaterial, cyan, 2.0, 0.9, 1);
    addLoop(g1, 6.2, 220, 'ring1', m, 0.9);
    const p = new Float32Array(4 * 6);
    for (let i = 0; i < 4; i++) {
      const a = (i / 4) * TAU, c = Math.cos(a), s = Math.sin(a);
      p.set([c * 5.75, s * 5.75, 0, c * 6.65, s * 6.65, 0], i * 6);
    }
    const mt = mat(THREE.LineBasicMaterial, cyan, 2.4, 0.9, 1);
    g1.add(reg(new THREE.LineSegments(geoFrom(p), mt), 'ring1', 4, 2, mt, 0.9));
  }

  /* --- Anillo 2 (r 7.4): 24 arcos discontinuos --- */
  const g2 = new THREE.Group();
  {
    const ARCS = 24, SUB = 5, span = (TAU / ARCS) * 0.62, r = 7.4;
    const p = new Float32Array(ARCS * SUB * 2 * 3);
    let o = 0;
    for (let i = 0; i < ARCS; i++) {
      const a0 = (i / ARCS) * TAU;
      for (let j = 0; j < SUB; j++) {
        const t0 = a0 + (j / SUB) * span, t1 = a0 + ((j + 1) / SUB) * span;
        p.set([Math.cos(t0) * r, Math.sin(t0) * r, 0, Math.cos(t1) * r, Math.sin(t1) * r, 0], o);
        o += 6;
      }
    }
    const m = mat(THREE.LineBasicMaterial, deep, 1.8, 0.8);
    g2.add(reg(new THREE.LineSegments(geoFrom(p), m), 'ring2', ARCS * SUB, 2, m, 0.8));
  }

  /* --- Anillo 3 (r 8.8): dial de 144 ticks (uno de cada 6, largo) --- */
  const g3 = new THREE.Group();
  {
    const TICKS = 144, r0 = 8.8;
    const p = new Float32Array(TICKS * 6);
    for (let i = 0; i < TICKS; i++) {
      const a = (i / TICKS) * TAU, r1 = r0 + (i % 6 === 0 ? 0.75 : 0.28);
      p.set([Math.cos(a) * r0, Math.sin(a) * r0, 0, Math.cos(a) * r1, Math.sin(a) * r1, 0], i * 6);
    }
    const m = mat(THREE.LineBasicMaterial, cyan, 1.7, 0.75, 0.35);
    g3.add(reg(new THREE.LineSegments(geoFrom(p), m), 'ring3', TICKS, 2, m, 0.75));
  }

  /* --- Anillo 4 (r 10.4): 3 arcos gruesos (uno dorado) --- */
  const g4 = new THREE.Group();
  [[0.25, 1.35, cyan, 0], [2.35, 0.95, gold, 1], [4.1, 1.7, cyan, 0.4]].forEach(([a0, len, hex, gw]) => {
    const SEG = 56;
    const m = mat(THREE.MeshBasicMaterial, hex, 2.0, 0.9, gw, { side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(new THREE.RingGeometry(10.3, 10.45, SEG, 1, a0, len), m);
    g4.add(reg(mesh, 'ring4', SEG, 6, m, 0.9));
  });

  /* --- Anillo 5 (r 12.6): círculo tenue + 4 cruces cardinales --- */
  const g5 = new THREE.Group();
  {
    const m = mat(THREE.LineBasicMaterial, deep, 1.4, 0.5);
    addLoop(g5, 12.6, 256, 'ring5', m, 0.5);
    const p = new Float32Array(4 * 6);
    for (let i = 0; i < 4; i++) {
      const a = (i / 4) * TAU + Math.PI / 4, c = Math.cos(a), s = Math.sin(a);
      p.set([c * 12.1, s * 12.1, 0, c * 13.2, s * 13.2, 0], i * 6);
    }
    const mt = mat(THREE.LineBasicMaterial, cyan, 2.0, 0.8);
    g5.add(reg(new THREE.LineSegments(geoFrom(p), mt), 'ring5', 4, 2, mt, 0.8));
  }

  /* --- Giroscopio: 2 aros inclinados que giran en 3D alrededor de la esfera --- */
  const gyro = new THREE.Group();
  {
    const a = new THREE.Group(), b = new THREE.Group();
    addLoop(a, 4.9, 160, 'gyro', mat(THREE.LineBasicMaterial, cyan, 1.8, 0.55, 0.6), 0.55);
    addLoop(b, 5.3, 160, 'gyro', mat(THREE.LineBasicMaterial, deep, 1.6, 0.45), 0.45);
    a.rotation.set(1.25, 0, 0.3);
    b.rotation.set(0.35, 1.1, 0);
    gyro.add(a, b);
  }

  group.add(g1, g2, g3, g4, g5, gyro);

  /* --- Cometas orbitales (cabeza + estela) --- */
  const dotMat = new THREE.ShaderMaterial({
    vertexShader: DOT_VERT, fragmentShader: POINT_FRAG,
    uniforms: { ...U, uOpacity: { value: 0 }, uSize: { value: 0.34 } },
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending
  });
  const comets = [];
  const makeComet = (r, speed, isGold) => {
    const n = 20, sign = Math.sign(speed);
    const pos = new Float32Array(n * 3), rnd = new Float32Array(n * 4);
    for (let i = 0; i < n; i++) {
      const a = -sign * i * 0.034;
      pos.set([Math.cos(a) * r, Math.sin(a) * r, 0], i * 3);
      rnd.set([0, 1 - (i / n) * 0.7, isGold ? 1 : 0, 1 - i / n], i * 4);
    }
    const geo = geoFrom(pos);
    geo.setAttribute('aRand', new THREE.BufferAttribute(rnd, 4));
    const pts = new THREE.Points(geo, dotMat);
    pts.frustumCulled = false;
    const holder = new THREE.Group();
    holder.add(pts);
    holder.rotation.z = Math.random() * TAU;
    holder.userData.speed = speed;
    group.add(holder);
    comets.push(holder);
  };
  makeComet(12.6, 0.9, false);
  makeComet(10.4, -0.65, false);
  makeComet(7.4, 1.5, true);

  return {
    object: group,
    update(dt, t, st) {
      for (const d of draws) {
        const p = st[d.key];
        d.obj.visible = p > 0.001;
        if (!d.obj.visible) continue;
        d.obj.geometry.setDrawRange(0, Math.round(p * d.n) * d.per);
        d.m.opacity = d.base * Math.min(1, p * 5);
      }
      for (const k of tints) k.m.color.lerpColors(k.a, k.b, st.gold * k.w);

      const s = st.spin;
      g1.rotation.z += dt * 0.30 * s;
      g2.rotation.z -= dt * 0.38 * s;
      g3.rotation.z += dt * 0.20 * s;
      g4.rotation.z -= dt * 0.14 * s;
      g5.rotation.z += dt * 0.05 * s;
      gyro.rotation.y += dt * (0.2 + 0.45 * s);
      gyro.rotation.z += dt * 0.12 * s;

      dotMat.uniforms.uOpacity.value = st.nodes;
      for (const c of comets) c.rotation.z += dt * c.userData.speed * (0.25 + 0.75 * Math.min(s, 2.5));
    }
  };
}

/* ==========================================================================
 * 4) RADAR (disco con estela angular) · ONDAS DE CHOQUE · RESPLANDOR DEL NÚCLEO
 * ========================================================================== */
function createSweep(U) {
  const R = 12;
  const u = { ...U, uAng: { value: 0 }, uOpacity: { value: 0 }, uR: { value: R } };
  const mat = new THREE.ShaderMaterial({
    uniforms: u,
    vertexShader: /* glsl */`
      uniform float uR;
      varying vec2 vP;
      void main() {
        vP = position.xy / uR;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: /* glsl */`
      uniform vec3  uColA, uColB;
      uniform float uAng, uOpacity, uGold, uIntensity;
      varying vec2 vP;
      void main() {
        float r = length(vP);
        float ang = atan(vP.y, vP.x);
        float d = mod(uAng - ang, 6.2831853);
        float tail = pow(clamp(1.0 - d / 1.5, 0.0, 1.0), 2.2);
        float edge = smoothstep(0.035, 0.0, d);
        float radial = smoothstep(0.45, 0.62, r) * (1.0 - smoothstep(0.92, 1.0, r));
        float a = (tail * 0.30 + edge * 0.85) * radial * uOpacity;
        gl_FragColor = vec4(mix(uColA, uColB, uGold) * uIntensity, a);
      }`,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending
  });
  const mesh = new THREE.Mesh(new THREE.CircleGeometry(R, 96), mat);
  let ang = 0;
  return {
    object: mesh,
    update(dt, t, st) {
      ang += dt * 2.1 * (0.25 + st.sweep);
      u.uAng.value = ang % TAU;
      u.uOpacity.value = st.sweep;
      mesh.visible = st.sweep > 0.001;
    }
  };
}

function createShocks(cfg) {
  const group = new THREE.Group();
  const make = (hex, k) => {
    const m = new THREE.MeshBasicMaterial({
      color: hdr(hex, k), transparent: true, opacity: 0,
      depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(new THREE.RingGeometry(0.985, 1.0, 160), m);
    mesh.visible = false;
    group.add(mesh);
    return mesh;
  };
  const s1 = make(cfg.colors.cyan, 2.2);
  const s2 = make(cfg.colors.gold, 3.0);
  const apply = (mesh, p, max, strength) => {
    mesh.visible = p > 0 && p < 1;
    if (!mesh.visible) return;
    mesh.scale.setScalar(2 + p * max);
    mesh.material.opacity = Math.pow(1 - p, 1.6) * strength;
  };
  return {
    object: group,
    update(dt, t, st) {
      apply(s1, st.shock1, 24, 0.55);
      apply(s2, st.shock2, 34, 0.85);
    }
  };
}

function createCoreGlow(U) {
  const u = { ...U, uOpacity: { value: 0 } };
  const mat = new THREE.ShaderMaterial({
    uniforms: u,
    vertexShader: /* glsl */`
      varying vec2 vUv;
      void main() {
        vUv = uv * 2.0 - 1.0;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: /* glsl */`
      uniform vec3  uColA, uColB;
      uniform float uGold, uIntensity, uOpacity;
      varying vec2 vUv;
      void main() {
        float d = length(vUv);
        float g = pow(max(1.0 - d, 0.0), 2.5);
        float hot = pow(max(1.0 - d * 3.0, 0.0), 2.0);
        gl_FragColor = vec4(mix(uColA, uColB, uGold) * uIntensity, (g * 0.5 + hot * 0.6) * uOpacity);
      }`,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending
  });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(18, 18), mat);
  return {
    object: mesh,
    update(dt, t, st) {
      u.uOpacity.value = Math.min(1, st.core) * 0.55;
      mesh.scale.setScalar(Math.max(0.01, st.core) * (1 + 0.04 * Math.sin(t * 3) + st.pulse * 0.5));
    }
  };
}

/* ==========================================================================
 * 5) MOTOR: renderer + escena + bloom + bucle requestAnimationFrame
 * ========================================================================== */
function createEngine(els, cfg, state) {
  const renderer = new THREE.WebGLRenderer({
    canvas: els.canvas,
    antialias: false,
    powerPreference: 'high-performance',
    alpha: false
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 1);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(cfg.camera.fov, 1, 0.1, 300);
  camera.position.z = state.camZ;

  // Uniforms compartidos por todos los shaders
  const U = {
    uTime: { value: 0 },
    uScale: { value: 600 },
    uIntensity: { value: cfg.intensity },
    uGold: { value: 0 },
    uColA: { value: new THREE.Color(cfg.colors.cyan) },
    uColB: { value: new THREE.Color(cfg.colors.gold) }
  };

  const world = new THREE.Group();   // escala "fit" (pantallas estrechas)
  const rig = new THREE.Group();     // inclinación por puntero (paralaje)
  world.add(rig);
  scene.add(world);

  const parts = [
    createCoreGlow(U), createShell(cfg, U), createHud(cfg, U),
    createSweep(U), createShocks(cfg)
  ];
  parts.forEach(p => rig.add(p.object));
  const dust = createDust(cfg, U);
  scene.add(dust.object);

  // Post-procesado: Render → Bloom → Output
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), cfg.bloom.start, cfg.bloom.radius, cfg.bloom.threshold);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  // Paralaje por puntero (suavizado)
  const ptr = { x: 0, y: 0, cx: 0, cy: 0 };
  const onPointer = e => {
    ptr.x = e.clientX / window.innerWidth - 0.5;
    ptr.y = e.clientY / window.innerHeight - 0.5;
  };
  window.addEventListener('pointermove', onPointer);

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();

    const t = Math.tan(THREE.MathUtils.degToRad(cfg.camera.fov / 2));
    U.uScale.value = (h * renderer.getPixelRatio()) / (2 * t);   // mundo → píxeles (tamaño de puntos)
    const viewH = 2 * t * cfg.camera.z, viewW = viewH * camera.aspect;
    world.scale.setScalar(Math.min(1, (viewW * 0.92) / 27));     // el anillo exterior (≈ 27 u) cabe siempre
  }
  resize();
  window.addEventListener('resize', resize);

  let raf = 0, last = 0, time = 0;
  function frame(now) {
    raf = requestAnimationFrame(frame);
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    time += dt;

    U.uTime.value = time;
    U.uGold.value = state.gold;

    const k = Math.min(1, dt * 3);
    ptr.cx += (ptr.x - ptr.cx) * k;
    ptr.cy += (ptr.y - ptr.cy) * k;
    rig.rotation.y = ptr.cx * 0.26 + Math.sin(time * 0.35) * 0.05;
    rig.rotation.x = ptr.cy * 0.18 + Math.cos(time * 0.28) * 0.035;

    camera.position.z = state.camZ;
    camera.rotation.z = state.roll;

    for (const p of parts) p.update(dt, time, state);
    dust.update(dt, time, state);

    bloom.strength = state.bloom + state.flash * cfg.bloom.flash;
    composer.render(dt);
  }

  return {
    start() { last = performance.now(); raf = requestAnimationFrame(frame); },
    dispose() {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', resize);
      window.removeEventListener('pointermove', onPointer);
      scene.traverse(o => {
        o.geometry?.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => m.dispose());
      });
      bloom.dispose();
      composer.dispose?.();
      renderer.dispose();
    }
  };
}

/* ==========================================================================
 * 6) TEXTO: título "decode" y escritura de estado
 * ========================================================================== */
const GLYPHS = '01ABCDEFGHKLMNPRSTUXZ#<>/\\|+=*';
const glyph = () => GLYPHS[(Math.random() * GLYPHS.length) | 0];

/** Divide el título en <span class="ch"> de ancho fijo (el scramble no mueve el layout). */
function splitTitle(el) {
  const text = el.dataset.text || el.textContent.trim();
  el.dataset.text = text;
  el.setAttribute('aria-label', text);
  el.textContent = '';
  const chars = [...text].map(ch => {
    const s = document.createElement('span');
    s.className = 'ch';
    s.dataset.ch = ch;
    s.textContent = ch;
    s.setAttribute('aria-hidden', 'true');
    el.appendChild(s);
    return s;
  });
  chars.forEach(s => { s.style.width = s.getBoundingClientRect().width + 'px'; });
  return chars;
}

/** Añade a la timeline la escritura (typewriter) de `text` en `el`. */
function typeInto(tl, el, text, at, dur, ready = false) {
  const p = { n: 0 };
  tl.to(p, {
    n: text.length, duration: dur, ease: 'none',
    onStart: () => { el.classList.toggle('ready', ready); },
    onUpdate: () => {
      const n = Math.round(p.n);
      el.textContent = text.slice(0, n) + (n < text.length ? '_' : '');
    },
    onComplete: () => { el.textContent = text; }
  }, at);
}

/* ==========================================================================
 * 7) SECUENCIA GSAP
 * --------------------------------------------------------------------------
 * Tiempos en "segundos nominales" sobre 6 s. `k = 6 / duration` escala todo
 * con timeScale (duration = 8 ⇒ ~8 s manteniendo proporciones).
 *
 *  0.00  Ignición: línea de eje, onda de choque, el núcleo se enciende
 *  0.30  Las partículas convergen en espiral → esfera · la cámara se acerca
 *  0.70  Los anillos se trazan uno a uno (1 → 5 + giroscopio)
 *  0.90  El título se decodifica letra a letra · barra y contadores suben
 *  1.90  Cometas orbitales · 2.00 radar
 *  4.35  READY: ráfaga, onda dorada, flash de bloom, ondulación dorada en el título
 *  5.40  Fade: la cámara atraviesa el HUD (warp) y todo se funde
 * ========================================================================== */
function buildMainTimeline(state, els, cfg, k, ctx) {
  const tl = gsap.timeline({ paused: true });
  const E = 'power3.out';

  /* ----- Ignición ----- */
  if (els.axis) tl.fromTo(els.axis, { scaleX: 0, opacity: 1 }, { scaleX: 1, duration: 1.0, ease: 'expo.out' }, 0.1)
                  .to(els.axis, { opacity: 0.14, duration: 1.4, ease: 'power2.out' }, 1.0);
  tl.to(state, { core: 1, duration: 1.0, ease: E }, 0.05);
  tl.to(state, { shock1: 1, duration: 1.5, ease: 'power3.out' }, 0.3);
  tl.to(state, { dust: 1, duration: 1.4, ease: 'none' }, 0);
  tl.to(state, { camZ: cfg.camera.z, roll: 0, duration: 3.4, ease: 'power2.out' }, 0);
  tl.to(state, { bloom: cfg.bloom.full, duration: 2.6, ease: 'power2.out' }, 0);

  /* ----- Esfera de partículas ----- */
  tl.to(state, { shell: 1, duration: 0.9, ease: 'none' }, 0.3);
  tl.to(state, { form: 1, duration: 2.5, ease: 'power2.inOut' }, 0.3);

  /* ----- Anillos: se trazan escalonados ----- */
  const draw = (key, at, dur = 1.2) => tl.to(state, { [key]: 1, duration: dur, ease: 'power3.inOut' }, at);
  draw('ring1', 0.70);
  draw('ring2', 0.95);
  draw('ring3', 1.20);
  draw('ring4', 1.45);
  draw('ring5', 1.70, 1.3);
  draw('gyro', 1.00, 1.5);
  tl.to(state, { spin: 1, duration: 2.6, ease: 'power2.out' }, 0.7);
  tl.to(state, { nodes: 1, duration: 0.8, ease: 'none' }, 1.9);
  tl.to(state, { sweep: 1, duration: 0.9, ease: 'power2.out' }, 2.0);

  /* ----- Líneas de escaneo (DOM) ----- */
  if (els.scan) {
    tl.fromTo(els.scan, { y: 0, opacity: 0.9 }, { y: window.innerHeight, opacity: 0, duration: 1.3, ease: 'power2.inOut' }, 1.0);
    tl.call(() => els.scan.classList.add('gold'), null, T_READY - 0.05);
    tl.fromTo(els.scan, { y: window.innerHeight, opacity: 0.95 }, { y: 0, opacity: 0, duration: 0.9, ease: 'power3.out' }, T_READY);
  }

  /* ----- Esquinas HUD ----- */
  if (els.corners?.length) {
    tl.fromTo(els.corners, { opacity: 0, scale: 0.8 },
      { opacity: 1, scale: 1, duration: 0.9, ease: E, stagger: 0.13 }, 0.5);
  }

  /* ----- Título: se cierra el espaciado + decode letra a letra ----- */
  tl.fromTo(els.title, { '--ls': '0.95em' }, { '--ls': '0.38em', duration: 2.4, ease: 'power3.out' }, 0.8);
  ctx.chars.forEach((span, i) => {
    const at = 0.9 + i * 0.12, p = { v: 0 }, fin = span.dataset.ch;
    let step = -1;
    tl.fromTo(span, { opacity: 0, scale: 1.5 }, { opacity: 1, scale: 1, duration: 0.35, ease: E }, at);
    tl.to(p, {
      v: 1, duration: 0.55, ease: 'none',
      onUpdate: () => {
        const s = Math.floor(p.v * 14);
        if (s !== step) { step = s; span.textContent = p.v < 1 ? glyph() : fin; }
      },
      onComplete: () => { span.textContent = fin; }
    }, at);
  });

  /* ----- Estado, barra y contadores ----- */
  typeInto(tl, els.status, cfg.text.init, 0.25, 0.6);
  typeInto(tl, els.status, cfg.text.load, 1.75, 0.65);
  typeInto(tl, els.status, cfg.text.sync, 3.05, 0.8);

  const prog = { v: 0 };
  tl.to(prog, {
    v: 100, duration: T_READY - 0.1, ease: 'power1.inOut',
    onUpdate: () => {
      const v = prog.v;
      if (els.bar) els.bar.style.transform = `scaleX(${(v / 100).toFixed(4)})`;
      if (els.pct) els.pct.textContent = String(Math.round(v)).padStart(3, '0') + '%';
      if (els.mod) els.mod.textContent = 'MÓD ' + String(Math.round((v / 100) * cfg.modules)).padStart(2, '0') + '/' + cfg.modules;
    }
  }, 0.1);

  /* ----- READY: ráfaga + pulso dorado ----- */
  tl.call(() => { ctx.ready = true; }, null, T_READY);
  typeInto(tl, els.status, cfg.text.ready, T_READY, 0.35, true);

  tl.to(state, { spin: 6.5, duration: 0.35, ease: 'power2.in' }, T_READY - 0.4);   // el HUD acelera…
  tl.to(state, { spin: 0.6, duration: 1.1, ease: 'power3.out' }, T_READY);         // …y se asienta
  tl.to(state, { pulse: 1, duration: 0.16, ease: 'power2.out' }, T_READY);
  tl.to(state, { pulse: 0, duration: 0.9, ease: 'elastic.out(1,0.45)' }, T_READY + 0.16);
  tl.to(state, { flash: 1, duration: 0.1, ease: 'power4.out' }, T_READY);
  tl.to(state, { flash: 0, duration: 0.9, ease: 'power2.out' }, T_READY + 0.1);
  tl.to(state, { shock2: 1, duration: 1.4, ease: 'expo.out' }, T_READY);
  tl.to(state, { gold: 1, duration: 0.5, ease: 'power2.out' }, T_READY);
  tl.to(state, { sweep: 0, duration: 0.7, ease: 'power2.out' }, T_READY - 0.1);
  tl.to(state, { bloom: cfg.bloom.end, duration: 1.0, ease: 'power2.inOut' }, T_READY + 0.45);

  // Título: parpadeo duro + ola dorada letra a letra
  tl.fromTo(els.title, { opacity: 1 },
    { opacity: 0.2, duration: 0.05, repeat: 5, yoyo: true, ease: 'steps(1)' }, T_READY + 0.02);
  tl.to(els.title, { '--tc': 'rgb(255, 201, 51)', scale: 1.05, duration: 0.6, ease: 'power2.out' }, T_READY + 0.3);
  tl.to(ctx.chars, { color: '#fff1c2', duration: 0.18, ease: 'power2.out', yoyo: true, repeat: 1, stagger: 0.07 }, T_READY + 0.1);

  tl.set({}, {}, MAIN_END);              // la timeline dura exactamente MAIN_END
  tl.timeScale(k);
  return tl;
}

function buildFadeTimeline(state, els, cfg, k) {
  const tl = gsap.timeline({ paused: true });
  tl.to(els.root, { autoAlpha: 0, duration: FADE_LEN, ease: 'power2.in' }, 0);
  tl.to(state, {
    camZ: cfg.camera.z - 17, warp: 1, bloom: cfg.bloom.full * 1.15,
    duration: FADE_LEN, ease: 'power3.in'
  }, 0);
  tl.to(els.title, { scale: 1.4, duration: FADE_LEN, ease: 'power3.in' }, 0);
  tl.timeScale(k);
  return tl;
}

const finished = tl => new Promise(res => tl.eventCallback('onComplete', res));

/* ==========================================================================
 * 8) API PÚBLICA
 * ========================================================================== */
let current = null;

/**
 * Reproduce la secuencia de arranque.
 * @param {object}   [opts]
 * @param {number}   [opts.duration]    Duración total en segundos (por defecto 6).
 * @param {Promise}  [opts.waitFor]     El fade-out espera a esta promesa (p. ej. audio/datos).
 *                                      El HUD queda en "SYSTEM READY" mientras tanto.
 * @param {Function} [opts.onFadeStart] Se llama justo al empezar el fade-out: ideal para
 *                                      revelar/animar la app por debajo.
 * @param {object}   [opts.config]      Sobrescribe cualquier valor de DEFAULTS.
 * @returns {Promise<void>} Se resuelve al terminar el fade-out y liberar la GPU.
 */
export function play(opts = {}) {
  if (current) return current;
  current = run(opts).finally(() => { current = null; });
  return current;
}

async function run(opts) {
  const cfg = merge(DEFAULTS, opts.config);
  const duration = opts.duration ?? cfg.duration;
  const k = NOMINAL_TOTAL / duration;

  const q = s => document.querySelector(s);
  const els = Object.fromEntries(
    Object.entries(cfg.selectors).filter(([n]) => n !== 'corners').map(([n, s]) => [n, q(s)])
  );
  els.corners = [...document.querySelectorAll(cfg.selectors.corners)];
  if (!els.root || !els.canvas) throw new Error('[boot] Faltan .boot-fx / #boot-canvas en el DOM');
  if (!els.title || !els.status) throw new Error('[boot] Faltan .boot-title / .boot-status en el DOM');

  const hexEl = q('[data-tele="hex"]'), timeEl = q('[data-tele="time"]');

  // Tipografía lista (medimos el ancho de cada letra); con tope por si la fuente tarda
  if (document.fonts?.ready) await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 400))]);

  // Estado inicial del DOM (también sirve para repetir la secuencia)
  gsap.set(els.root, { autoAlpha: 1 });
  gsap.set(els.title, { opacity: 1, scale: 1, '--tc': 'rgb(0, 212, 255)', '--ls': '0.95em' });
  els.status.classList.remove('ready');
  els.status.textContent = '';
  els.scan?.classList.remove('gold');
  if (els.bar) els.bar.style.transform = 'scaleX(0)';
  if (els.pct) els.pct.textContent = '000%';
  if (els.mod) els.mod.textContent = 'MÓD 00/' + cfg.modules;
  if (els.axis) gsap.set(els.axis, { opacity: 0, scaleX: 0 });
  if (els.scan) gsap.set(els.scan, { opacity: 0, y: 0 });
  if (els.corners.length) gsap.set(els.corners, { opacity: 0 });
  gsap.set(els.hud, { opacity: 1 });

  const ctx = { chars: splitTitle(els.title), ready: false };
  gsap.set(ctx.chars, { opacity: 0, color: '#eaffff' });

  // Telemetría decorativa (hex que cambia + reloj)
  const t0 = performance.now();
  const tele = setInterval(() => {
    if (timeEl) timeEl.textContent = 'T+' + ((performance.now() - t0) / 1000).toFixed(2) + 's';
    if (!hexEl) return;
    if (ctx.ready) { hexEl.textContent = 'ONLINE'; hexEl.classList.add('ready'); return; }
    hexEl.classList.remove('ready');
    hexEl.textContent = '0x' + ((Math.random() * 0xffffff) | 0).toString(16).toUpperCase().padStart(6, '0');
  }, 70);

  const state = makeState(cfg);

  // Si WebGL no está disponible (algunos webviews Linux), la secuencia sigue con el HUD en DOM.
  let engine = null;
  try {
    engine = createEngine(els, cfg, state);
  } catch (err) {
    console.warn('[boot] WebGL no disponible, se usa solo el HUD:', err);
  }

  const main = buildMainTimeline(state, els, cfg, k, ctx);
  const fade = buildFadeTimeline(state, els, cfg, k);

  engine?.start();
  const mainDone = finished(main);
  main.play(0);
  await mainDone;

  // HUD en "SYSTEM READY" hasta que lo que haga falta (audio, datos…) termine
  if (opts.waitFor) await Promise.resolve(opts.waitFor).catch(() => {});

  opts.onFadeStart?.();
  const fadeDone = finished(fade);
  fade.play(0);
  await fadeDone;

  clearInterval(tele);
  engine?.dispose();
  main.kill(); fade.kill();
  gsap.set(els.root, { autoAlpha: 0 });
}

// Acceso global para scripts clásicos (no módulo):  window.SelliumBoot.play({ ... })
if (typeof window !== 'undefined') window.SelliumBoot = { play };