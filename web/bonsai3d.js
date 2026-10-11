// A bonsai that grows from the self-improvement loop, one limb per proposed harness change.
//
// Every limb's live state (length, thickness, fall transform, wilt) sits in one float texture, so
// the GPU grows, thickens, sways and drops branches without rebuilding geometry. The tree has no
// size limit: new limbs fill gaps in the crown, trunk height saturates like a real tree, and older
// limbs thicken by the pipe model (a limb is as thick as what it carries).
import * as THREE from 'three';

const TEX_W = 1024, PER = 8;
const V = (x = 0, y = 0, z = 0) => new THREE.Vector3(x, y, z);
const UP = V(0, 1, 0);

let seed = 7;
const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const rr = (a, b) => a + rnd() * (b - a);
function hash(x, y, z) { const h = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453; return h - Math.floor(h); }
function noise3(x, y, z) {
  const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z), xf = x - xi, yf = y - yi, zf = z - zi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf), w = zf * zf * (3 - 2 * zf), L = (a, b, t) => a + (b - a) * t, c = (i, j, k) => hash(xi + i, yi + j, zi + k);
  return L(L(L(c(0,0,0), c(1,0,0), u), L(c(0,1,0), c(1,1,0), u), v), L(L(c(0,0,1), c(1,0,1), u), L(c(0,1,1), c(1,1,1), u), v), w) * 2 - 1;
}
const fbm = (x, y, z) => noise3(x, y, z) * .6 + noise3(x * 2.1, y * 2.1, z * 2.1) * .3 + noise3(x * 4.3, y * 4.3, z * 4.3) * .1;
function randDir() { const u = rnd() * 2 - 1, a = rnd() * Math.PI * 2, s = Math.sqrt(1 - u * u); return V(s * Math.cos(a), u, s * Math.sin(a)); }
const clamp01 = x => Math.min(1, Math.max(0, x));

// ---------- shaders ----------
const NOISE = `
float h3(vec3 p) { p = fract(p * .3183099 + .1); p *= 17.; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
float vn(vec3 x) { vec3 i = floor(x), f = fract(x); f = f * f * (3. - 2. * f);
  return mix(mix(mix(h3(i), h3(i + vec3(1,0,0)), f.x), mix(h3(i + vec3(0,1,0)), h3(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(h3(i + vec3(0,0,1)), h3(i + vec3(1,0,1)), f.x), mix(h3(i + vec3(0,1,1)), h3(i + vec3(1,1,1)), f.x), f.y), f.z); }`;
const COMMON = `
uniform sampler2D uLimbs; uniform float uTime; uniform float uWind; uniform float uSize;
vec4 limbT(float id, int k) { int i = int(id + .5) * ${PER} + k; return texelFetch(uLimbs, ivec2(i % ${TEX_W}, i / ${TEX_W}), 0); }
mat4 limbM(float id) { return mat4(limbT(id, 2), limbT(id, 3), limbT(id, 4), limbT(id, 5)); }
vec3 windOff(vec3 p) {
  float sc = 4.5 / max(uSize, 1.), h = max(p.y + .1, 0.) * sc, r = length(p.xz) * sc;
  float gust = .55 + .45 * sin(uTime * .31) * sin(uTime * .17 + 1.3) + .2 * sin(uTime * 2.1 + p.x);
  float k = uWind * gust * (.0035 * h * h + .012 * r * r) / sc;
  return vec3(sin(uTime * 1.25 + p.y * .5 + p.z * .3) * k + uWind * .004 * h * h / sc, 0., cos(uTime * .93 + p.x * .4) * k * .6);
}
vec3 placeP(vec3 p, float id) { if (limbT(id, 1).x > .5) return (limbM(id) * vec4(p, 1.)).xyz; return p + windOff(p); }`;
// JS twin of windOff, for the few things placed on the CPU (cut caps, shadow proxies).
function windOff(p, t, wind, size, out = V()) {
  const sc = 4.5 / Math.max(size, 1), h = Math.max(p.y + .1, 0) * sc, r = Math.hypot(p.x, p.z) * sc;
  const gust = .55 + .45 * Math.sin(t * .31) * Math.sin(t * .17 + 1.3) + .2 * Math.sin(t * 2.1 + p.x);
  const k = wind * gust * (.0035 * h * h + .012 * r * r) / sc;
  return out.set(Math.sin(t * 1.25 + p.y * .5 + p.z * .3) * k + wind * .004 * h * h / sc, 0, Math.cos(t * .93 + p.x * .4) * k * .6);
}

const WOOD_V = `
attribute float aLimb; attribute vec3 aCenter; attribute vec3 aDir; attribute float aS; attribute float aR; attribute float aAng; attribute float aLobe;
varying float vAng; varying float vS; varying float vTwig;
float woodRadius() {
  vec4 t0 = limbT(aLimb, 0), t1 = limbT(aLimb, 1), t6 = limbT(aLimb, 6), t7 = limbT(aLimb, 7);
  float s = aS + t1.y;
  float win = t7.w > 0. ? clamp((t7.w - (t0.w - s)) / .5, 0., 1.) : 1.;
  if (aR < 0.) return -aR * clamp((t0.w - s) / .12, 0., 1.) * t7.z * t6.w * win;
  if (s > t0.w + 1e-4) return 0.;
  float x = clamp(s / max(t0.x, 1e-3), 0., 1.);
  float r = t0.z + (t0.y - t0.z) * pow(1. - x, t1.z > 0. ? t1.z : .85);
  r *= 1. + t7.x * exp(-s / .07) + t7.y * exp(-s / .4) * (.4 + aLobe);
  return r * aR * t6.w;
}`;
const WOOD_BEGIN = `
  vec3 transformed = placeP(aCenter + aDir * woodRadius(), aLimb);
  vAng = aAng; vS = aS + limbT(aLimb, 1).y; vTwig = aR < 0. ? smoothstep(.04, .012, -aR) : 0.;`;
const WOOD_NORMAL = `
  vec3 objectNormal = aDir;
  if (limbT(aLimb, 1).x > .5) objectNormal = mat3(limbM(aLimb)) * aDir;`;
const WOOD_F = `
uniform vec3 uBark; uniform vec3 uBarkDark; uniform vec3 uLichen; uniform vec3 uTwigC;
varying float vAng; varying float vS; varying float vTwig;
${NOISE}
float barkH() {
  vec2 q = vec2(cos(vAng), sin(vAng));
  float plates = vn(vec3(q * 1.8, vS * .8)) * .65 + vn(vec3(q * 4., vS * 2.2)) * .35;
  float fis = 1. - abs(vn(vec3(q * 4.5, vS * 1.1) + 3.) * 2. - 1.);
  float fine = vn(vec3(q * 18., vS * 9.));
  return clamp(.42 + .45 * plates - .5 * pow(fis, 9.) + .1 * fine, 0., 1.);
}
vec3 bumpN(vec3 p, vec3 n, float hx, float hy) { vec3 sx = dFdx(p), sy = dFdy(p), r1 = cross(sy, n), r2 = cross(n, sx); float d = dot(sx, r1); return normalize(abs(d) * n - sign(d) * (hx * r1 + hy * r2)); }`;
const WOOD_COLOR = `
  float hb = barkH() * (1. - vTwig);
  vec3 bc = mix(uBarkDark, uBark, smoothstep(.1, .62, hb));
  float lich = smoothstep(.64, .72, vn(vec3(cos(vAng) * 3., sin(vAng) * 3., vS * 2.2) + 11.)) * (1. - vTwig);
  bc = mix(bc, uLichen, lich * .3);
  diffuseColor.rgb = mix(bc, uTwigC, vTwig);`;
const WOOD_BUMP = `
  normal = bumpN(-vViewPosition, normal, dFdx(hb) * .55, dFdy(hb) * .55);`;

const NEEDLE_V = `attribute float aLimb; attribute vec3 aBase; attribute float aS; uniform vec3 uCandle; uniform vec3 uDead;`;
const NEEDLE_BEGIN = `
  vec4 t0 = limbT(aLimb, 0), t1 = limbT(aLimb, 1), t6 = limbT(aLimb, 6), t7 = limbT(aLimb, 7);
  float s = aS + t1.y;
  float win = t7.w > 0. ? clamp((t7.w - (t0.w - s)) / .5, 0., 1.) : 1.;
  float g = clamp((t0.w - s) / .22, 0., 1.); g = 1. - pow(1. - g, 3.);
  vec3 transformed = aBase + (position - aBase) * g;
  float tip = step(.0001, distance(position, aBase));
  transformed += tip * uWind * .006 * vec3(sin(uTime * 6.3 + aBase.x * 41.), 0., cos(uTime * 5.1 + aBase.z * 29.));
  transformed.y -= tip * t6.x * .05;                                   // wilting needles sag
  transformed = placeP(transformed, aLimb);
  #ifdef USE_COLOR_ALPHA
    vColor.rgb = mix(vColor.rgb, uCandle, (1. - t6.y) * .7);           // new growth is pale until it matures
    vColor.rgb = mix(vColor.rgb, uDead, t6.x);                         // on the ground it browns
    vColor.a *= step(.001, g) * t6.z * win;
  #endif`;

const FLOWER_V = `attribute float aLimb; attribute float aS; uniform vec3 uDead;`;
const FLOWER_BEGIN = `
  vec4 t0 = limbT(aLimb, 0), t1 = limbT(aLimb, 1), t6 = limbT(aLimb, 6), t7 = limbT(aLimb, 7);
  float s = aS + t1.y;
  float win = t7.w > 0. ? clamp((t7.w - (t0.w - s)) / .5, 0., 1.) : 1.;
  float x = clamp((t0.w - s) / .3, 0., 1.);
  float g = 1. + 2.7 * pow(x - 1., 3.) + 1.7 * pow(x - 1., 2.);
  vec3 transformed = position * g * t6.z * win;
  transformed.y -= t6.x * .35 * length(position.xz);                  // petals droop as they wilt
  transformed.y += sin(uTime * 4.7 + aS * 33.) * .1 * uWind * length(position.xz);`;
const FLOWER_PROJECT = `
  vec4 wp = vec4(transformed, 1.);
  #ifdef USE_INSTANCING
    wp = instanceMatrix * wp;
  #endif
  wp.xyz = placeP(wp.xyz, aLimb);
  vec4 mvPosition = modelViewMatrix * wp;
  gl_Position = projectionMatrix * mvPosition;
  #ifdef USE_COLOR
    vColor.rgb = mix(vColor.rgb, uDead, t6.x);
  #endif`;

const CAP_F = `
${NOISE}`;
const CAP_COLOR = `
  vec2 c = vUv - .5; float rad = length(c) * 2., ang = atan(c.y, c.x);
  float rings = abs(fract(rad * 9. + vn(vec3(c * 5., 2.)) * .7) * 2. - 1.);
  vec3 wood = mix(vec3(.78, .64, .45), vec3(.62, .47, .3), smoothstep(.0, .65, 1. - rad));
  wood *= mix(.8, 1.04, smoothstep(.15, .5, rings));
  wood *= 1. - .35 * smoothstep(.06, .0, rad);
  float crack = smoothstep(.97, 1., vn(vec3(ang * 3.2, rad * 2., 5.))) * smoothstep(.2, .7, rad);
  diffuseColor.rgb = wood * (1. - .5 * crack);`;

function patch(mat, {vpars = '', begin, normal, project, fpars = '', color, bump, cacheKey}) {
  mat.onBeforeCompile = sh => {
    Object.assign(sh.uniforms, mat.userData.U);
    sh.vertexShader = sh.vertexShader.replace('#include <common>', '#include <common>\n' + COMMON + vpars);
    if (begin) sh.vertexShader = sh.vertexShader.replace('#include <begin_vertex>', begin);
    if (normal) sh.vertexShader = sh.vertexShader.replace('#include <beginnormal_vertex>', normal);
    if (project) sh.vertexShader = sh.vertexShader.replace('#include <project_vertex>', project);
    if (fpars) sh.fragmentShader = sh.fragmentShader.replace('#include <common>', '#include <common>\n' + fpars);
    if (color) sh.fragmentShader = sh.fragmentShader.replace('#include <color_fragment>', '#include <color_fragment>\n' + color);
    if (bump) sh.fragmentShader = sh.fragmentShader.replace('#include <normal_fragment_maps>', '#include <normal_fragment_maps>\n' + bump);
  };
  mat.customProgramCacheKey = () => cacheKey;
  return mat;
}

// ---------- shapes ----------
function flowerGeo() {
  const pos = [], col = [], idx = [], centre = new THREE.Color(0xfff6f1), edge = new THREE.Color(0xf2a3b7), disk = new THREE.Color(0xe2bd4a);
  const add = (x, y, z, c) => { pos.push(x, y, z); col.push(c.r, c.g, c.b); return pos.length / 3 - 1; };
  for (let p = 0; p < 5; p++) {
    const phi = p / 5 * Math.PI * 2 + .2, c0 = add(0, .02, 0, centre), N = 12, ring = [];
    for (let i = 0; i <= N; i++) {
      const u = i / N * 2 - 1, th = phi + u * .62;
      const r = Math.pow(Math.cos(u * Math.PI / 2), .45) * (1 - .22 * Math.exp(-((u / .14) ** 2)));
      ring.push(add(Math.cos(th) * r, .28 * r * r, Math.sin(th) * r, centre.clone().lerp(edge, Math.min(1, r * 1.1))));
      const m = add(Math.cos(th) * r * .5, .07 * r, Math.sin(th) * r * .5, centre.clone().lerp(edge, .35));
      ring.push(m);
    }
    for (let i = 0; i < N; i++) { const o = ring[i * 2], om = ring[i * 2 + 1], n = ring[i * 2 + 2], nm = ring[i * 2 + 3]; idx.push(c0, om, nm, om, o, n, om, n, nm); }
  }
  const dc = add(0, .06, 0, disk);
  for (let i = 0; i < 10; i++) {                       // stamens: little raised tips around the centre
    const a = i / 10 * Math.PI * 2, b = add(Math.cos(a) * .08, .05, Math.sin(a) * .08, disk), t = add(Math.cos(a) * .22, .2, Math.sin(a) * .22, disk.clone().multiplyScalar(.75));
    const b2 = add(Math.cos(a + .25) * .08, .05, Math.sin(a + .25) * .08, disk); idx.push(dc, b, b2, b, t, b2);
  }
  const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3)); g.setIndex(idx); g.computeVertexNormals();
  return g;
}
function leafGeo() {
  const pos = [], col = [], idx = [], base = new THREE.Color(0x6f9a4c), tip = new THREE.Color(0xa9c97a), N = 10;
  pos.push(0, 0, 0); col.push(base.r, base.g, base.b);
  for (let i = 0; i <= N; i++) {
    const u = i / N, w = Math.sin(u * Math.PI) * .32 * (1 + .08 * Math.sin(u * 40));
    for (const sgn of [-1, 1]) { const c = base.clone().lerp(tip, u); pos.push(u, .12 * u * u + .05 * w, sgn * w); col.push(c.r, c.g, c.b); }
  }
  for (let i = 0; i < N; i++) { const a = 1 + i * 2; idx.push(0, a, a + 2, 0, a + 3, a + 1, a, a + 1, a + 2, a + 1, a + 3, a + 2); }
  const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3)); g.setIndex(idx); g.computeVertexNormals();
  return g;
}

// ---------- rigid body for a cut-off piece ----------
class Body {
  constructor(points) {
    this.c0 = points.reduce((a, p) => a.add(p), V()).multiplyScalar(1 / points.length);
    this.local = points.map(p => p.clone().sub(this.c0));
    const I = new THREE.Matrix3(), e = I.elements, m = 1 / points.length;
    let xx = 0, yy = 0, zz = 0, xy = 0, xz = 0, yz = 0;
    for (const r of this.local) { xx += m * (r.y * r.y + r.z * r.z); yy += m * (r.x * r.x + r.z * r.z); zz += m * (r.x * r.x + r.y * r.y); xy -= m * r.x * r.y; xz -= m * r.x * r.z; yz -= m * r.y * r.z; }
    I.set(xx + .01, xy, xz, xy, yy + .01, yz, xz, yz, zz + .01);
    this.Iinv0 = I.invert();
    this.x = this.c0.clone(); this.q = new THREE.Quaternion(); this.v = V(); this.w = V();
    this.calm = 0; this.settled = false; this.age = 0;
  }
  step(dt, ground) {
    if (this.settled) return;
    this.age += dt;
    this.v.y -= 7.5 * dt;
    this.x.addScaledVector(this.v, dt);
    const wq = new THREE.Quaternion(this.w.x * dt * .5, this.w.y * dt * .5, this.w.z * dt * .5, 0).multiply(this.q);
    this.q.set(this.q.x + wq.x, this.q.y + wq.y, this.q.z + wq.z, this.q.w + wq.w).normalize();
    const R = new THREE.Matrix3().setFromMatrix4(new THREE.Matrix4().makeRotationFromQuaternion(this.q));
    const Iinv = R.clone().multiply(this.Iinv0).multiply(R.clone().transpose());
    let contact = false, lift = 0;
    for (const l of this.local) {
      const r = l.clone().applyMatrix3(R), p = r.clone().add(this.x), h = ground(p.x, p.z);
      if (p.y >= h) continue;
      contact = true; lift = Math.max(lift, h - p.y);
      const vp = this.v.clone().add(this.w.clone().cross(r)), vn = vp.y;
      if (vn >= 0) continue;
      const rn = r.clone().cross(UP), kn = 1 + UP.dot(rn.clone().applyMatrix3(Iinv).cross(r));
      const j = -(1.25) * vn / kn;                                     // restitution .25: a small bounce
      this.v.y += j; this.w.add(rn.clone().multiplyScalar(j).applyMatrix3(Iinv));
      const vt = vp.clone().setY(0), sp = vt.length();
      if (sp > 1e-4) {
        const tdir = vt.multiplyScalar(1 / sp), rt = r.clone().cross(tdir), kt = 1 + tdir.dot(rt.clone().applyMatrix3(Iinv).cross(r));
        const jt = Math.max(-sp / kt, -.7 * j);                        // friction, capped by the normal push
        this.v.addScaledVector(tdir, jt); this.w.add(rt.multiplyScalar(jt).applyMatrix3(Iinv));
      }
    }
    if (contact) { this.x.y += lift * .8; this.w.multiplyScalar(.985); this.v.x *= .99; this.v.z *= .99; }
    this.calm = contact && this.v.length() < .08 && this.w.length() < .3 ? this.calm + dt : 0;
    if (this.calm > .6 || this.age > 12) { this.settled = true; this.v.set(0, 0, 0); this.w.set(0, 0, 0); }
  }
  matrix(out) { return out.makeRotationFromQuaternion(this.q).setPosition(this.x).multiply(new THREE.Matrix4().makeTranslation(-this.c0.x, -this.c0.y, -this.c0.z)); }
}

// ---------- the tree ----------
export function createBonsai({scene, form = 'moyogi', foliage = 'pine', accent = '#b5332a', onLabel = () => {}}) {
  seed = form === 'moyogi' ? 7 : 19;
  const G = new THREE.Group(); scene.add(G);
  let rows = 16;
  let data = new Float32Array(TEX_W * rows * 4);
  const U = {
    uLimbs:{value:null}, uTime:{value:0}, uWind:{value:1}, uSize:{value:4.5},
    uCandle:{value:new THREE.Color(0xd3da8e)}, uDead:{value:new THREE.Color(0x8f7044)},
    uBark:{value:new THREE.Color(foliage === 'pine' ? 0x8a7768 : 0x6a5853)}, uBarkDark:{value:new THREE.Color(foliage === 'pine' ? 0x3b2f28 : 0x2a201e)},
    uLichen:{value:new THREE.Color(0xa9ab9c)}, uTwigC:{value:new THREE.Color(foliage === 'pine' ? 0x5f4c3f : 0x3f3230)},
  };
  const makeTex = () => { const t = new THREE.DataTexture(data, TEX_W, rows, THREE.RGBAFormat, THREE.FloatType); t.minFilter = t.magFilter = THREE.NearestFilter; t.needsUpdate = true; U.uLimbs.value = t; };
  makeTex();
  const mats = {
    wood: patch(new THREE.MeshStandardMaterial({roughness:.92, metalness:0}), {vpars:WOOD_V, begin:WOOD_BEGIN, normal:WOOD_NORMAL, fpars:WOOD_F, color:WOOD_COLOR, bump:WOOD_BUMP, cacheKey:'wood'}),
    woodDepth: patch(new THREE.MeshDepthMaterial({depthPacking:THREE.RGBADepthPacking}), {vpars:WOOD_V, begin:WOOD_BEGIN, cacheKey:'woodD'}),
    needle: patch(new THREE.LineBasicMaterial({vertexColors:true, transparent:true, depthWrite:false}), {vpars:NEEDLE_V, begin:NEEDLE_BEGIN, cacheKey:'needle'}),
    flower: patch(new THREE.MeshStandardMaterial({vertexColors:true, side:THREE.DoubleSide, roughness:.65}), {vpars:FLOWER_V, begin:FLOWER_BEGIN, project:FLOWER_PROJECT, cacheKey:'flower'}),
    flowerDepth: patch(new THREE.MeshDepthMaterial({depthPacking:THREE.RGBADepthPacking}), {vpars:FLOWER_V, begin:FLOWER_BEGIN, project:FLOWER_PROJECT, cacheKey:'flowerD'}),
    cap: patch(new THREE.MeshStandardMaterial({roughness:.8}), {fpars:CAP_F, color:CAP_COLOR, cacheKey:'cap'}),
    seal: new THREE.MeshStandardMaterial({color:new THREE.Color(accent), roughness:.35}),
    callus: new THREE.MeshStandardMaterial({color:U.uBark.value.clone().multiplyScalar(.9), roughness:.95}),
    proxy: new THREE.MeshBasicMaterial({colorWrite:false, depthWrite:false}),
  };
  for (const k of ['wood', 'woodDepth', 'needle', 'flower', 'flowerDepth', 'cap']) mats[k].userData.U = U;
  mats.cap.defines = {USE_UV:''};
  const FLOWER = flowerGeo(), LEAF = leafGeo();

  // --- limbs and their texture rows ---
  const limbs = [], free = [];
  function newLimb(props) {
    const id = free.length ? free.pop() : limbs.length;
    if ((id + 1) * PER > TEX_W * rows) { rows *= 2; const d = new Float32Array(TEX_W * rows * 4); d.set(data); data = d; U.uLimbs.value.dispose(); makeTex(); }
    const l = Object.assign({id, L:0, Ltarget:0, rate:1, Lmain:1e9, Lp:null, Lvis:null, thick:.03, thickTarget:.03, base:.03, rTip:.01, collar:0, flare:0, sOff:0, window:0, thickScale:1,
      falling:false, body:null, M:new THREE.Matrix4(), maturity:1, matTarget:1, wilt:0, alpha:1, decomp:1, objs:[], pts:[], ss:[], parent:null, desc:0, alive:true}, props);
    limbs[id] = l; return l;
  }
  function writeLimb(l) {
    const o = l.id * PER * 4, Lvis = l.Lvis ?? l.L;
    data.set([Math.min(l.Lp ?? l.L, l.Lmain), l.thick, l.rTip, Lvis, l.falling ? 1 : 0, l.sOff, l.taper || .85, 0], o);
    data.set(l.M.elements, o + 8);
    data.set([l.wilt, l.maturity, l.alpha, l.decomp, l.collar, l.flare, l.thickScale, l.window], o + 24);
  }
  function freeLimb(l) {
    l.alive = false; l.objs.forEach(o => { G.remove(o); o.geometry?.dispose(); }); l.objs = [];
    l.L = 0; l.Lvis = 0; l.thick = 0; l.falling = false; l.M.identity(); writeLimb(l); free.push(l.id);
  }
  const pointOn = (l, s) => {
    const ss = l.ss; if (s <= ss[0]) return l.pts[0].clone();
    for (let i = 1; i < ss.length; i++) if (ss[i] >= s) return l.pts[i - 1].clone().lerp(l.pts[i], (s - ss[i - 1]) / (ss[i] - ss[i - 1] || 1));
    return l.pts.at(-1).clone();
  };
  const tangentOn = (l, s) => { const a = pointOn(l, s - .03), b = pointOn(l, s + .03); return b.sub(a).normalize(); };
  const radiusAt = (l, sAbs) => {
    const Lp = Math.min(l.Lp ?? l.L, l.Lmain), x = clamp01(sAbs / Math.max(Lp, 1e-3));
    return (l.rTip + (l.thick - l.rTip) * Math.pow(1 - x, l.taper || .85)) * (1 + l.collar * Math.exp(-sAbs / .07) + l.flare * Math.exp(-sAbs / .4) * .9) * l.decomp;
  };

  // --- geometry builders ---
  function tube(buf, pts, ss, {radial = 10, bark = .1, twigR = null, lobes = false, gapAt = -1, sd = 0}) {
    const n = pts.length, base = buf.cen.length / 3, T = V(), Nm = V(), B = V();
    const t0 = pts[1].clone().sub(pts[0]).normalize();
    Nm.copy(Math.abs(t0.y) < .9 ? UP : V(1, 0, 0)).cross(t0).normalize();
    for (let i = 0; i < n; i++) {
      T.copy(pts[Math.min(i + 1, n - 1)]).sub(pts[Math.max(i - 1, 0)]).normalize();
      Nm.sub(T.clone().multiplyScalar(Nm.dot(T))).normalize(); B.crossVectors(T, Nm);
      for (let k = 0; k < radial; k++) {
        const a = k / radial * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a), d = Nm.clone().multiplyScalar(ca).addScaledVector(B, sa);
        const nz = fbm(ca * 1.7 + sd, sa * 1.7, ss[i] * .9);
        buf.cen.push(pts[i].x, pts[i].y, pts[i].z); buf.dir.push(d.x, d.y, d.z); buf.s.push(ss[i]);
        buf.r.push(twigR ? -twigR[i] : 1 + bark * nz); buf.ang.push(a + sd * 7); buf.lobe.push(lobes ? Math.pow(Math.max(0, Math.sin(a * 3 + .7)), 2) : 0);
      }
    }
    for (let i = 0; i < n - 1; i++) if (i !== gapAt) for (let k = 0; k < radial; k++) {
      const a = base + i * radial + k, b = base + i * radial + (k + 1) % radial;
      buf.idx.push(a, b, a + radial, b, b + radial, a + radial);
    }
  }
  const newBuf = () => ({cen:[], dir:[], s:[], r:[], ang:[], lobe:[], idx:[]});
  function woodMesh(buf, l) {
    if (!buf.idx.length) return;
    const g = new THREE.BufferGeometry(), f = (a, k) => new THREE.Float32BufferAttribute(a, k);
    g.setAttribute('position', f(buf.cen, 3)); g.setAttribute('aCenter', f(buf.cen, 3)); g.setAttribute('aDir', f(buf.dir, 3));
    g.setAttribute('aS', f(buf.s, 1)); g.setAttribute('aR', f(buf.r, 1)); g.setAttribute('aAng', f(buf.ang, 1)); g.setAttribute('aLobe', f(buf.lobe, 1));
    g.setAttribute('aLimb', f(new Array(buf.s.length).fill(l.id), 1)); g.setIndex(buf.idx);
    const m = new THREE.Mesh(g, mats.wood); m.customDepthMaterial = mats.woodDepth; m.castShadow = true; m.frustumCulled = false;
    G.add(m); l.objs.push(m);
  }
  function walk(start, dir, len, o = {}) {
    const steps = Math.max(3, Math.round(len / (o.step || .05))), pts = [start.clone()], d = dir.clone().normalize(), p = start.clone(), sd = rnd() * 50, f = o.freq || 1.6;
    for (let i = 1; i <= steps; i++) {
      d.add(V(noise3(p.x * f + sd, p.y * f, p.z * f), noise3(p.x * f, p.y * f + sd, p.z * f), noise3(p.x * f, p.y * f, p.z * f + sd)).multiplyScalar(o.gnarl ?? .25));
      if (o.pull) d.addScaledVector(o.pull, o.pullK ?? .08);
      d.normalize(); p.addScaledVector(d, len / steps);
      if (o.floor != null && p.y < o.floor) p.y = o.floor;
      pts.push(p.clone());
    }
    return pts;
  }
  // Grows toward a target the way a shoot does: leaves its parent outward, droops under its own weight, bends to the light.
  function steer(from, to, out0) {
    const dist = from.distanceTo(to), steps = Math.max(8, Math.round(dist / .045)), pts = [from.clone()], p = from.clone(), d = out0.clone().normalize(), sd = rnd() * 50;
    for (let i = 1; i <= steps; i++) {
      const f = i / steps, want = to.clone().sub(p).normalize();
      d.lerp(want, .06 + f * .35);
      d.add(V(noise3(p.x * 1.8 + sd, p.y * 1.8, p.z * 1.8), noise3(p.x * 1.8, p.y * 1.8 + sd, p.z * 1.8), noise3(p.x * 1.8, p.y * 1.8, p.z * 1.8 + sd)).multiplyScalar(.2));
      d.y -= .05 * Math.sin(f * Math.PI);
      d.normalize(); p.addScaledVector(d, dist / steps * 1.06); pts.push(p.clone());
    }
    return pts;
  }
  const arc = (pts, s0 = 0) => { const ss = [s0]; for (let i = 1; i < pts.length; i++) ss.push(ss[i - 1] + pts[i].distanceTo(pts[i - 1])); return ss; };

  // Foliage for one limb, gathered then turned into one line mesh (pine) or two instanced meshes (blossom).
  function foliageBuf() { return {pos:[], base:[], s:[], col:[], fl:[], lv:[]}; }
  const NL = new THREE.Color(0x86a26f), NM = new THREE.Color(0x45614a), ND = new THREE.Color(0x1a241d), NOLD = new THREE.Color(0xa08a4e);
  function tuft(fb, P, s, size, count, upK = 1.1) {
    for (let j = 0; j < count; j += 2) {                     // pine needles come in pairs from a short sheath
      const d = randDir(); d.y = Math.abs(d.y) * upK + .15; d.normalize();
      const b = P.clone().add(randDir().multiplyScalar(size * .1)), old = rnd() < .04;
      for (const sp of [-.13, .13]) {
        const dd = d.clone().applyAxisAngle(V(d.z, 0, -d.x).normalize(), sp), L = size * rr(.6, 1.1);
        const m = b.clone().addScaledVector(dd, L * .5), t = b.clone().addScaledVector(dd, L); t.y -= L * .08; m.y -= L * .01;
        const up = dd.y, c = old ? NOLD : up > .55 ? NM.clone().lerp(NL, (up - .55) / .45 * rr(.4, 1)) : ND.clone().lerp(NM, up / .55 * rr(.6, 1));
        const cb = c.clone().multiplyScalar(.7), a = rr(.55, .9);
        for (const [q, cc] of [[b, cb], [m, c], [m, c], [t, c.clone().multiplyScalar(1.12)]]) { fb.pos.push(q.x, q.y, q.z); fb.base.push(b.x, b.y, b.z); fb.s.push(s); fb.col.push(cc.r, cc.g, cc.b, a); }
      }
    }
  }
  function blossom(fb, P, s, density) {
    const n = Math.round(rr(1, 3) * density + .3);
    for (let i = 0; i < n; i++) {
      const p = P.clone().add(randDir().multiplyScalar(rr(.02, .07))); p.y += rr(0, .04);
      const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(rr(-.5, .5), rr(0, 6.28), rr(-.5, .5)));
      const bud = rnd() < .15, sc = bud ? rr(.018, .025) : rr(.045, .068);
      fb.fl.push({p, q, sc, s:s + rr(0, .15), c:bud ? 0xc0466e : [0xffffff, 0xfff0f4, 0xffe2ea, 0xffd2de][Math.floor(rnd() * 4)]});
    }
    if (rnd() < .45 * density) {
      const p = P.clone().add(randDir().multiplyScalar(.04)), q = new THREE.Quaternion().setFromEuler(new THREE.Euler(rr(-.4, .2), rr(0, 6.28), rr(-.6, .6)));
      fb.lv.push({p, q, sc:rr(.07, .1), s:s + .1, c:0xffffff});
    }
  }
  function foliageAt(fb, P, s, density) { foliage === 'pine' ? tuft(fb, P, s, rr(.11, .17), Math.round(rr(40, 60) * density)) : blossom(fb, P, s, density * 1.3); }
  function foliageMesh(fb, l) {
    if (fb.pos.length) {
      const g = new THREE.BufferGeometry(), f = (a, k) => new THREE.Float32BufferAttribute(a, k);
      g.setAttribute('position', f(fb.pos, 3)); g.setAttribute('aBase', f(fb.base, 3)); g.setAttribute('aS', f(fb.s, 1));
      g.setAttribute('color', f(fb.col, 4)); g.setAttribute('aLimb', f(new Array(fb.s.length).fill(l.id), 1));
      const m = new THREE.LineSegments(g, mats.needle); m.frustumCulled = false; G.add(m); l.objs.push(m);
    }
    for (const [list, geo] of [[fb.fl, FLOWER], [fb.lv, LEAF]]) {
      if (!list.length) continue;
      const g = geo.clone(), n = list.length;
      g.setAttribute('aLimb', new THREE.InstancedBufferAttribute(new Float32Array(n).fill(l.id), 1));
      g.setAttribute('aS', new THREE.InstancedBufferAttribute(new Float32Array(list.map(x => x.s)), 1));
      const im = new THREE.InstancedMesh(g, mats.flower, n), M = new THREE.Matrix4(), c = new THREE.Color();
      list.forEach((x, i) => { im.setMatrixAt(i, M.compose(x.p, x.q, V(x.sc, x.sc, x.sc))); im.setColorAt(i, c.setHex(x.c)); });
      im.customDepthMaterial = mats.flowerDepth; im.castShadow = true; im.frustumCulled = false; G.add(im); l.objs.push(im);
    }
  }
  // A pad: twigs fanning out flat from the end of a limb, forking, needles or blossom all along, domed on top.
  function pad(buf, fb, l, ptsB, ssB, size, density) {
    const end = ptsB.at(-1), dir = end.clone().sub(ptsB[Math.max(0, ptsB.length - 6)]), flat = V(dir.x, 0, dir.z).normalize();
    if (!isFinite(flat.x)) flat.set(1, 0, 0);
    const centre = end.clone().addScaledVector(flat, size * .3).add(V(0, size * .12, 0));
    const twigs = Math.round(7 + size * 9);
    let reach = 0;
    for (let i = 0; i < twigs; i++) {
      const k = Math.floor(ptsB.length * rr(.5, 1)) - 1, at = ptsB[Math.max(0, k)], s0 = ssB[Math.max(0, k)];
      const td = flat.clone().applyAxisAngle(UP, rr(-1.45, 1.45)).add(V(0, rr(-.05, .25), 0)), len = size * rr(.4, .95);
      const pts = walk(at, td, len, {gnarl:.35, step:.04, pull:UP, pullK:.03}), ss = arc(pts, s0);
      tube(buf, pts, ss, {radial:5, twigR:pts.map((_, j) => .024 - .019 * j / (pts.length - 1)), sd:i});
      for (let j = Math.floor(pts.length * .2); j < pts.length; j += 2) {
        const p = pts[j].clone(), dc = p.distanceTo(centre) / size; p.y += size * .16 * Math.max(0, 1 - dc * dc);
        foliageAt(fb, p, ss[j], density);
      }
      for (let f = 0; f < 2; f++) {
        const m = Math.floor(pts.length * rr(.35, .75)), fd = td.clone().applyAxisAngle(UP, (f ? 1 : -1) * rr(.5, .9));
        const fp = walk(pts[m], fd, len * rr(.3, .5), {gnarl:.4, step:.04, pull:UP, pullK:.04}), fs = arc(fp, ss[m]);
        tube(buf, fp, fs, {radial:4, twigR:fp.map((_, j) => .011 - .008 * j / (fp.length - 1)), sd:i + f});
        for (let j = 1; j < fp.length; j += 2) foliageAt(fb, fp[j].clone().add(V(0, size * .07, 0)), fs[j], density * .75);
        reach = Math.max(reach, fs.at(-1));
      }
      reach = Math.max(reach, ss.at(-1));
    }
    return {centre, reach};
  }

  // --- pot, soil, ground ---
  const pot = new THREE.Group(); pot.position.y = -.07; G.add(pot);
  const POT_W = 1.75, POT_D = 1.15;
  {
    const ink = c => new THREE.MeshStandardMaterial({color:c, roughness:.85});
    const shape = new THREE.Shape(), w = POT_W, d = POT_D, r = .2, depth = .5;
    shape.moveTo(-w + r, -d); shape.lineTo(w - r, -d); shape.quadraticCurveTo(w, -d, w, -d + r); shape.lineTo(w, d - r); shape.quadraticCurveTo(w, d, w - r, d);
    shape.lineTo(-w + r, d); shape.quadraticCurveTo(-w, d, -w, d - r); shape.lineTo(-w, -d + r); shape.quadraticCurveTo(-w, -d, -w + r, -d);
    const g = new THREE.ExtrudeGeometry(shape, {depth, bevelEnabled:true, bevelThickness:.04, bevelSize:.04, bevelSegments:3, curveSegments:8});
    g.rotateX(-Math.PI / 2); g.translate(0, -.05 - depth, 0);
    const pa = g.attributes.position;
    for (let i = 0; i < pa.count; i++) { const k = .86 + .14 * clamp01((pa.getY(i) + .05 + depth) / depth); pa.setX(i, pa.getX(i) * k); pa.setZ(i, pa.getZ(i) * k); }
    g.computeVertexNormals();
    const body = new THREE.Mesh(g, ink(form === 'moyogi' ? 0x3a332e : 0x4a3a33)); body.castShadow = true; pot.add(body);
    for (const [x, z] of [[-1.26, -.75], [1.26, -.75], [-1.26, .75], [1.26, .75]]) { const f = new THREE.Mesh(new THREE.BoxGeometry(.3, .14, .22), ink(0x2a2420)); f.position.set(x, -.6, z); f.castShadow = true; pot.add(f); }
    const soil = new THREE.Mesh(new THREE.PlaneGeometry(3.3, 2.1), ink(0x2e2720)); soil.rotation.x = -Math.PI / 2; soil.position.y = -.03; pot.add(soil);
    const mp = [], mc = [], green = [new THREE.Color(0x56704a), new THREE.Color(0x3c5236), new THREE.Color(0x7d915e), new THREE.Color(0x8a8a5a)];
    for (let i = 0; i < 12000; i++) {
      const x = rr(-1.6, 1.6), z = rr(-1, 1);
      if (fbm(x * 1.4, z * 1.4, 4) < -.25) continue;
      mp.push(x, -.02 + Math.max(0, .12 - Math.hypot(x, z * 1.4) * .07) + fbm(x * 3, z * 3, 0) * .03, z); const c = green[i % 4]; mc.push(c.r, c.g, c.b);
    }
    const mg = new THREE.BufferGeometry(); mg.setAttribute('position', new THREE.Float32BufferAttribute(mp, 3)); mg.setAttribute('color', new THREE.Float32BufferAttribute(mc, 3));
    pot.add(new THREE.Points(mg, new THREE.PointsMaterial({size:.032, vertexColors:true})));
    for (const [x, z, s] of [[1.05, .35, .2], [1.3, .1, .12], [-1.15, -.45, .16]]) {
      const st = new THREE.Mesh(new THREE.DodecahedronGeometry(s, 1), ink(0x77716a)); st.position.set(x, -.01, z); st.scale.y = .6; st.rotation.set(rnd(), rnd(), rnd()); st.castShadow = true; pot.add(st);
    }
  }
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(80, 80), new THREE.ShadowMaterial({color:0x3a2e20, opacity:.08}));
  floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; G.add(floor);
  let potScale = 1;
  const floorY = () => -.07 - .67 * potScale;
  const ground = (x, z) => Math.abs(x) < (POT_W + .05) * potScale && Math.abs(z) < (POT_D + .05) * potScale ? -.08 : floorY();

  // --- trunk, roots, crown sprigs ---
  const HMAX = 5.4, TN = 240;
  const tpath = form === 'moyogi'
    ? t => V(Math.sin(t * 4 + .3) * .85 - t * .35, -.12 + t * HMAX, Math.cos(t * 3) * .4 - .4)
    : t => V(t * 2.9 + Math.sin(t * 3) * .25, -.12 + t * HMAX * .78 - t * t * .5, Math.sin(t * 2) * .2);
  const tpts = []; for (let i = 0; i < TN; i++) { const t = i / (TN - 1); tpts.push(tpath(t).add(V(fbm(t * 3, 1, 0) * .12, 0, fbm(t * 3, 5, 0) * .12))); }
  const trunk = newLimb({kind:'trunk', pts:tpts, ss:arc(tpts), thick:.16, thickTarget:.16, base:.16, rTip:.035, flare:.45, taper:1.5, window:0, rate:.6});
  const TLEN = trunk.ss.at(-1);
  { const b = newBuf(); tube(b, tpts, trunk.ss, {radial:36, bark:.16, lobes:true, sd:3}); woodMesh(b, trunk); }
  const roots = newLimb({kind:'roots', thick:1, rTip:1, rate:.5, Lmain:0});
  { const b = newBuf();
    for (let i = 0; i < 7; i++) {
      const a = i / 7 * Math.PI * 2 + rr(-.3, .3), pts = walk(V(tpts[0].x, .02, tpts[0].z), V(Math.cos(a), -.3, Math.sin(a)), rr(.5, 1.05), {gnarl:.22, pull:V(0, -1, 0), pullK:.05, floor:-.12});
      tube(b, pts, arc(pts), {radial:10, twigR:pts.map((_, j) => .09 * Math.pow(1 - j / (pts.length - 1), .9) + .012), sd:i + 9});
    }
    woodMesh(b, roots); roots.pts = [tpts[0]]; roots.ss = [0]; }
  const sprigs = newLimb({kind:'sprigs', pts:tpts, ss:trunk.ss, thick:0, rTip:0, window:1.5, maturity:1, rate:.6});
  { const b = newBuf(), fb = foliageBuf();
    for (let s = .8; s < TLEN; s += .2) {
      const at = pointOn(trunk, s), a = s * 2.4, d = V(Math.cos(a), .55, Math.sin(a));
      const pts = walk(at, d, rr(.22, .42), {gnarl:.35, step:.04, pull:UP, pullK:.05}), ss = arc(pts, s);
      tube(b, pts, ss, {radial:5, twigR:pts.map((_, j) => .02 - .015 * j / (pts.length - 1))});
      for (let j = 1; j < pts.length; j += 2) foliageAt(fb, pts[j], ss[j], .9);
    }
    woodMesh(b, sprigs); foliageMesh(fb, sprigs); }

  // --- crown bookkeeping ---
  const pads = [], attempts = {}, sites = {}, keptLimbs = [trunk], caps = [], proxies = [], pieces = [];
  let nKept = 0, now = 0;
  const tipY = () => pointOn(trunk, Math.max(trunk.L, .3)).y;
  const trunkAtY = y => { let best = tpts[0]; for (let i = 0; i < TN && trunk.ss[i] <= trunk.L; i++) if (Math.abs(tpts[i].y - y) < Math.abs(best.y - y)) best = tpts[i]; return best; };
  const emptiness = p => {
    let d = 9; for (const q of pads) d = Math.min(d, p.distanceTo(q.centre) - q.size * .55);
    const ax = trunkAtY(p.y); d = Math.min(d, Math.hypot(p.x - ax.x, p.z - ax.z) * 1.4 - .25);
    return d;
  };
  // Pick where the next shoot should head: the emptiest spot in the crown, near the parent version's pad.
  function pickTarget(near, avoid, parentPad) {
    const H = Math.max(tipY(), 1.2), lo = H * .42, hi = H * 1.04, W = .55 + .38 * H + .2 * Math.log(1 + nKept / 10);
    let best = null, bestScore = -1e9;
    for (let i = 0; i < 70; i++) {
      let p;
      if (near) { const d = randDir(); d.y = Math.abs(d.y) * .6 + .05; p = near.clone().addScaledVector(d.normalize(), rr(.7, 1.6)); if (avoid && d.dot(avoid) > .25) continue; }
      else {
        const h = rr(lo, hi), f = (h - lo) / (hi - lo), ax = trunkAtY(h);
        let a = rr(0, Math.PI * 2); if (form === 'windswept' && rnd() < .85) a = rr(-1.25, 1.25);
        const rad = Math.sqrt(rnd()) * (W * (1 - .72 * f) + .2);
        p = V(ax.x + Math.cos(a) * rad + (form === 'windswept' ? .6 * f * H * .3 : 0), h, ax.z + Math.sin(a) * rad);
      }
      if (p.y < .5) continue;
      let sc = emptiness(p);
      if (parentPad) sc -= .25 * p.distanceTo(parentPad.centre) / H;
      if (sc > bestScore) { bestScore = sc; best = p; }
    }
    return best || V(rr(-1, 1), tipY() * .7, rr(-1, 1));
  }
  // Pick where it leaves the existing tree: closest living kept wood below the target, the parent's own limb preferred.
  function pickAttach(target, parentId) {
    let best = null, bc = 1e9;
    for (const l of keptLimbs) {
      if (!l.alive || l.falling) continue;
      const s0 = l === trunk ? .45 : .05, s1 = l === trunk ? trunk.L - .25 : Math.min(l.L, l.Lmain) - .02;
      for (let s = s0; s < s1; s += .1) {
        const p = pointOn(l, s); if (p.y > target.y + .25) continue;
        const c = p.distanceTo(target) - (l.version === parentId ? .45 : 0) + (radiusAt(l, s) < .018 ? .6 : 0);
        if (c < bc) { bc = c; best = {limb:l, s, pos:p}; }
      }
    }
    return best || {limb:trunk, s:trunk.L * .6, pos:pointOn(trunk, trunk.L * .6)};
  }

  // A new shoot for one proposed change: stub (stays if pruned) + piece (falls if pruned), pad at the end.
  function makeAttempt(id, parentId) {
    const site = sites[parentId] ??= {stumps:[]}, stump = site.stumps.at(-1), parentPad = attempts[parentId]?.pad;
    let attach, target;
    if (stump) {                                                // a dormant bud just behind the last cut breaks and grows another way
      const s = stump.cutS * .7; attach = {limb:stump.A, s, pos:pointOn(stump.A, s)};
      target = pickTarget(attach.pos, stump.dir, null);
    } else { target = pickTarget(null, null, parentPad); attach = pickAttach(target, parentId); }
    const lim = 2.2 + .15 * Math.sqrt(nKept); if (target.distanceTo(attach.pos) > lim) target = attach.pos.clone().lerp(target, lim / target.distanceTo(attach.pos));
    const out = target.clone().sub(attach.pos); out.y = Math.max(out.y, 0) * .4 + .25; const tgAt = tangentOn(attach.limb, attach.s);
    out.addScaledVector(tgAt, -out.dot(tgAt) * .7);
    const path = steer(attach.pos, target, out), ss = arc(path), Lb = ss.at(-1);
    const cutS = Math.min(Math.max(.3 * Lb, .12), .45), cutI = Math.max(1, ss.findIndex(s => s >= cutS));
    const A = newLimb({kind:'stub', version:id, pts:path.slice(0, cutI + 1), ss:ss.slice(0, cutI + 1), parent:attach.limb, attachS:attach.s, collar:.45, rate:.9, thick:.012, thickTarget:.03, base:.03, rTip:.009, Lmain:Lb, maturity:0, matTarget:0});
    const ptsB = path.slice(cutI), ssB = ss.slice(cutI).map(s => s - ss[cutI]);
    const B = newLimb({kind:'piece', version:id, pts:ptsB, ss:ssB.map(s => s + ss[cutI]), parent:A, sOff:ss[cutI], rate:.9, thick:.012, thickTarget:.03, base:.03, rTip:.009, Lmain:Lb, maturity:0, matTarget:0});
    { const b = newBuf(); tube(b, A.pts, A.ss, {radial:14, bark:.1, sd:A.id}); woodMesh(b, A); }
    const size = (.6 + .45 * rnd()) * Math.pow(1 / (1 + nKept / 30), .3) + .15;
    const b = newBuf(), fb = foliageBuf();
    tube(b, ptsB, ssB, {radial:12, bark:.08, sd:B.id});
    const {centre, reach} = pad(b, fb, B, ptsB, ssB, size, Math.pow(1 / (1 + nKept / 40), .4));
    woodMesh(b, B); foliageMesh(fb, B);
    const proxy = new THREE.Mesh(new THREE.SphereGeometry(1, 14, 8), mats.proxy); proxy.castShadow = true; G.add(proxy);
    const a = {id, A, B, cutS:ss[cutI], dir:out.clone().normalize(), full:ss[cutI] + reach + .25, pad:{centre, size}, proxy, parentId, attach, grownAt:now};
    B.objs.push(proxy); proxies.push(a);
    A.Ltarget = B.Ltarget = .15;                               // a bud
    attempts[id] = a; return a;
  }
  function setGrowth(a, L) { a.A.Ltarget = a.B.Ltarget = Math.max(a.A.Ltarget, L); }

  // Pipe model: each kept shoot adds to everything it hangs from.
  function thicken(l) {
    for (let p = l; p; p = p.parent) {
      p.desc++;
      p.thickTarget = Math.min(p === trunk ? .85 : .5, p.base * Math.pow(1 + p.desc, p === trunk ? .32 : .45));
      if (p.parent && p.parent !== trunk && p.kind === 'stub') p.thickTarget = Math.min(p.thickTarget, radiusAt(p.parent, p.attachS) * .8);
    }
  }

  function snap(...ls) { for (const l of ls) { l.L = l.Ltarget; l.thick = l.thickTarget; } }
  function cut(a, signal, quiet = false) {
    const {A, B} = a;
    const L = A.L, cs = Math.min(a.cutS, Math.max(L - .02, .05));
    A.Lp = Math.max(L, .1); A.Lvis = Math.min(L, a.cutS); A.Ltarget = A.L;
    if (L <= a.cutS || quiet) { freeLimb(B); }
    else {
      B.Lp = Math.max(L, .1); B.Lvis = L; B.Ltarget = B.L; B.falling = true;
      const pts = [];
      B.pts.forEach((p, i) => { if (B.ss[i] <= L && i % 2 === 0) pts.push(p.clone()); });
      if (L > a.full * .6) for (let k = 0; k < 6; k++) { const ang = k / 6 * Math.PI * 2; pts.push(a.pad.centre.clone().add(V(Math.cos(ang) * a.pad.size * .8, -.05, Math.sin(ang) * a.pad.size * .8))); }
      if (pts.length < 3) pts.push(pointOn(B, L * .5), pointOn(B, L));
      B.body = new Body(pts);
      B.body.v.copy(a.dir).setY(0).multiplyScalar(.6); B.body.w.copy(UP.clone().cross(a.dir).normalize().multiplyScalar(1.4 + rnd()));
      pieces.push(B);
    }
    // the stump: exposed wood, a ring of cut paste, and callus that slowly rolls over the edge
    const capS = Math.min(a.cutS, A.Lvis) - .002, cap = new THREE.Group();
    const disk = new THREE.Mesh(new THREE.CircleGeometry(1, 32), mats.cap);
    const seal = new THREE.Mesh(new THREE.TorusGeometry(1, .09, 8, 32), mats.seal);
    const callus = new THREE.Mesh(new THREE.TorusGeometry(1.02, .16, 10, 32), mats.callus);
    disk.castShadow = seal.castShadow = callus.castShadow = true; cap.add(disk, seal, callus); G.add(cap); A.objs.push(cap);
    caps.push({A, s:capS, cap, callus, born:quiet ? now - 30 : now, dir:tangentOn(A, capS)});
    (sites[a.parentId] ??= {stumps:[]}).stumps.push({A, cutS:capS, dir:a.dir});
    if (!quiet) onLabel({text:`${a.id}  ${signal || 'pruned'}`, pos:pointOn(A, capS), cut:true});
  }

  // ---------- events from the loop ----------
  const api = {
    seed(v) { trunk.Ltarget = TLEN * .22; roots.Ltarget = 2; sprigs.Ltarget = trunk.Ltarget; sites[v.id] = {stumps:[]}; onLabel({text:`${v.id}  seed`, pos:V(0, .4, 0)}); },
    propose(id, parent) { if (!attempts[id] && trunk.L > .3) { const a = makeAttempt(id, parent); onLabel({text:`${id}  testing`, pos:a.pad.centre.clone().add(V(0, a.pad.size * .7, 0))}); } },
    progress(id, done, total) { const a = attempts[id]; if (a) setGrowth(a, (.15 + .75 * done / Math.max(total, 1)) * a.full * .85); },
    verdict(v, instant = false) {
      if (!v.parent) { api.seed(v); if (instant) snap(trunk, roots, sprigs); return; }
      const a = attempts[v.id] || makeAttempt(v.id, v.parent);
      if (v.kept) {
        a.A.Ltarget = a.B.Ltarget = a.full; a.A.matTarget = a.B.matTarget = 1;
        keptLimbs.push(a.A, a.B); pads.push(a.pad); nKept++; thicken(a.A); thicken(a.B);
        trunk.Ltarget = TLEN * (.22 + .78 * (1 - Math.exp(-nKept / 9))); sprigs.Ltarget = trunk.Ltarget;
        sites[v.id] ??= {stumps:[]};
        if (instant) { snap(a.A, a.B, trunk, sprigs); a.A.maturity = a.B.maturity = 1; }
        else onLabel({text:`${v.id}  ${Math.round(v.score * 100)}%`, pos:a.pad.centre.clone().add(V(0, -a.pad.size * .6, 0))});
      } else if (instant) { a.A.L = a.B.L = a.A.Ltarget = a.cutS + .05; cut(a, v.signal, true); }
      else setTimeout(() => cut(a, v.signal), 500);
    },
    shed(n = 1) { drift.burst(n); },
    focus(id) {                                                  // label the shoot of one version, for an agent being watched
      const a = attempts[id], last = api._focusAt || 0;
      if (now - last < 5 && api._focusId === id) return;
      api._focusAt = now; api._focusId = id;
      const pos = a ? a.pad.centre.clone().add(V(0, a.pad.size * .8, 0)) : pointOn(trunk, Math.max(trunk.L, .3)).add(V(0, .4, 0));
      onLabel({text:`watching ${id}`, pos});
    },
    bounds() {
      let top = tipY() + .4, r = 1.8;
      for (const p of pads) { top = Math.max(top, p.centre.y + p.size * .5); r = Math.max(r, Math.hypot(p.centre.x, p.centre.z) + p.size); }
      return {top, bottom:floorY(), radius:r};
    },
    get floorY() { return floorY(); },
    dispose() { G.traverse(o => o.geometry?.dispose()); scene.remove(G); U.uLimbs.value.dispose(); },
    update,
  };

  // ---------- drifting needles / petals ----------
  const drift = (() => {
    const geo = foliage === 'pine' ? new THREE.BoxGeometry(.005, .005, .13) : FLOWER.clone().scale(.05, .05, .05);
    const mat = new THREE.MeshStandardMaterial({color:foliage === 'pine' ? 0x9a8550 : 0xffe4ec, side:THREE.DoubleSide, vertexColors:foliage !== 'pine'});
    const n = 90, im = new THREE.InstancedMesh(geo, mat, n); im.frustumCulled = false; G.add(im);
    const P = Array.from({length:n}, () => ({on:false})), M = new THREE.Matrix4(), Z = new THREE.Matrix4().makeScale(0, 0, 0), Q = new THREE.Quaternion();
    for (let i = 0; i < n; i++) im.setMatrixAt(i, Z);
    const slow = foliage !== 'pine';
    let want = 0;
    function spawn() {
      const live = pads.length ? pads : [{centre:pointOn(trunk, trunk.L), size:.4}], o = live[Math.floor(rnd() * live.length)], p = P.find(p => !p.on);
      if (p) Object.assign(p, {on:true, pos:o.centre.clone().add(V(rr(-1, 1) * o.size * .7, rr(-.1, .1), rr(-1, 1) * o.size * .7)), vy:0, ph:rnd() * 6, rot:new THREE.Euler(rnd() * 6, rnd() * 6, rnd() * 6), life:0, landed:-1});
    }
    return {
      burst(k) { want += k * 3; },
      step(dt, wind) {
        if (trunk.L > .5 && rnd() < dt * (slow ? .9 : .6)) spawn();
        while (want > 0) { spawn(); want--; }
        P.forEach((p, i) => {
          if (!p.on) return;
          p.life += dt;
          if (p.landed < 0) {
            p.vy = Math.max(p.vy - dt * (slow ? .9 : 2.5), slow ? -.42 : -1.1);
            p.pos.y += p.vy * dt;
            p.pos.x += (Math.sin(p.life * (slow ? 2.4 : 4) + p.ph) * (slow ? .55 : .2) + wind * .25) * dt;
            p.pos.z += Math.cos(p.life * 1.9 + p.ph) * (slow ? .35 : .1) * dt;
            p.rot.x += dt * (slow ? 2.5 : 4); p.rot.z += dt * 1.7;
            const gy = ground(p.pos.x, p.pos.z) + .01;
            if (p.pos.y <= gy) { p.pos.y = gy; p.landed = p.life; p.rot.x = slow ? 0 : Math.PI / 2; }
          }
          const s = p.landed > 0 ? Math.max(0, 1 - (p.life - p.landed - 6) / 2) : 1;
          if (s <= 0) { p.on = false; im.setMatrixAt(i, Z); return; }
          im.setMatrixAt(i, M.compose(p.pos, Q.setFromEuler(p.rot), V(s, s, s)));
        });
        im.instanceMatrix.needsUpdate = true;
      },
    };
  })();

  // ---------- per frame ----------
  const tmp = V(), tmp2 = V(), Mx = new THREE.Matrix4();
  function update(dt, t, wind) {
    now = t; U.uTime.value = t; U.uWind.value = wind; U.uSize.value = Math.max(tipY(), 2.5);
    potScale += (Math.max(1, .75 + trunk.thick * 2.6) - potScale) * Math.min(1, dt * .5);
    pot.scale.setScalar(potScale); floor.position.y = floorY();
    for (const l of limbs) {
      if (!l || !l.alive) continue;
      if (!l.falling) {
        if (l.L < l.Ltarget) l.L = Math.min(l.Ltarget, l.L + l.rate * dt * (1 + (l.Ltarget - l.L)));
        l.thick += (l.thickTarget - l.thick) * Math.min(1, dt * .35);
        l.maturity += (l.matTarget - l.maturity) * Math.min(1, dt * .25);
        if (l.kind === 'roots') l.thickScale = trunk.thick / .12;
        else if (l.kind === 'piece' || l.kind === 'stub' || l.kind === 'sprigs') l.thickScale = Math.min(2.2, Math.sqrt(1 + l.desc));
      } else {
        const b = l.body;
        const n = Math.ceil(dt / .008); for (let k = 0; k < n; k++) b.step(dt / n, ground);
        b.matrix(l.M);
        if (b.settled) {
          b.rest = (b.rest || 0) + dt;
          l.wilt = clamp01(b.rest / 10); l.alpha = 1 - clamp01((b.rest - 8) / 18); l.decomp = 1 - clamp01((b.rest - 20) / 20);
          if (b.rest > 42) { freeLimb(l); pieces.splice(pieces.indexOf(l), 1); continue; }
        }
      }
      writeLimb(l);
    }
    U.uLimbs.value.needsUpdate = true;
    for (const c of caps) {
      if (!c.A.alive) continue;
      const p = pointOn(c.A, c.s), r = radiusAt(c.A, c.s) * 1.02;
      c.cap.position.copy(p).add(windOff(p, t, wind, U.uSize.value, tmp));
      c.cap.quaternion.setFromUnitVectors(V(0, 0, 1), c.dir);
      c.cap.scale.setScalar(Math.max(r, .004));
      const heal = clamp01((t - c.born) / 30);
      c.callus.scale.set(1 + .05 * heal, 1 + .05 * heal, .2 + 1.4 * heal); c.callus.position.z = -.1 + .04 * heal;
    }
    for (const a of proxies) {
      const B = a.B; if (!B.alive) continue;
      const g = clamp01((B.L - (a.full - .35)) / .3) * B.alpha;
      Mx.compose(a.pad.centre, new THREE.Quaternion(), V(a.pad.size * .95 * g + 1e-4, a.pad.size * .34 * g + 1e-4, a.pad.size * .85 * g + 1e-4));
      if (B.falling) a.proxy.matrix.copy(B.M).multiply(Mx); else { a.proxy.matrix.copy(Mx); a.proxy.matrix.elements[12] += windOff(a.pad.centre, t, wind, U.uSize.value, tmp2).x; }
      a.proxy.matrixAutoUpdate = false; a.proxy.matrixWorldNeedsUpdate = true;
    }
    drift.step(dt, wind);
  }

  return api;
}
