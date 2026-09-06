/* ==========================================================================
   A.R.S — the being
   services/gateway/src/ars_gateway/ui/entity/ars-entity.js

   This is A.R.S's face. Not a waveform, not a spinner: a crystalline lattice
   held inside a machined ring assembly, with a mechanical iris that dilates and
   neural filaments that fire between the core and the rings.

   Six states, each readable across a room with no label:

     idle       slow breathing, rings barely turning, iris half open
     listening  the whole assembly opens outward, motes are drawn INWARD,
                the level ring tracks setIntensity in real time
     thinking   core contracts hard, iris stops down to a slit, lattice fires
                in outward waves, filaments everywhere
     speaking   radial petals push OUT in time with intensity, motes exhale
     acting     rotation goes to discrete detents with a mechanical overshoot,
                actuator ticks extend — it is touching Alex's real accounts
     alert      rotation LOCKS, hazard chevrons, hard square-wave strobe,
                chromatic tear. It blocked something.

   Rendering: WebGL2. One fullscreen analytic pass (rings, lattice, iris — all
   signed-distance, so it is pin-sharp at 300px and at 4000px), one instanced
   pass for filaments, one for motes, then a half-res bloom. Canvas2D fallback
   if WebGL2 is missing. No build step, no dependencies, no network.

   The GPU is often busy running a local model. This renderer watches its own
   frame times and degrades itself — bloom off, fewer filaments, lower DPR —
   before it ever starves the model. It also stops completely when hidden.

   Colours come from theme.css (--ars-entity-*). Do not hardcode them here.

   API
     const ars = new ArsEntity(canvasEl, { size: 'auto', quality: 'auto' });
     ars.setState('idle'|'listening'|'thinking'|'speaking'|'acting'|'alert');
     ars.setIntensity(0..1);
     ars.setTier('recall'|'documents'|'local model'|'cloud model');
     ars.destroy();
   plus: pulse(), lookAt(x,y), setQuality(n|'auto'), refreshTheme(), stats()
   ========================================================================== */

export const ARS_STATES = ['idle', 'listening', 'thinking', 'speaking', 'acting', 'alert'];
export const ARS_TIERS = ['recall', 'documents', 'local model', 'cloud model'];

/* --- small maths ------------------------------------------------------- */

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const lerp = (a, b, t) => a + (b - a) * t;

/** Frame-rate independent exponential approach. tau in seconds. */
const approach = (cur, target, tau, dt) => cur + (target - cur) * (1 - Math.exp(-dt / Math.max(tau, 1e-4)));

/** A spring that overshoots a little then settles — the "machined" feel. */
class Spring {
  constructor(value, stiffness = 120, damping = 18) {
    this.v = value; this.x = value; this.target = value;
    this.k = stiffness; this.d = damping;
  }
  step(dt) {
    // sub-step so a long frame cannot explode the integration
    const steps = Math.min(4, Math.max(1, Math.ceil(dt / 0.008)));
    const h = dt / steps;
    for (let i = 0; i < steps; i++) {
      const a = (this.target - this.x) * this.k - this.v * this.d;
      this.v += a * h;
      this.x += this.v * h;
    }
    return this.x;
  }
}

/* --- colour ------------------------------------------------------------- */

function parseColor(str, fallback) {
  if (!str) return fallback;
  const s = str.trim();
  let m = s.match(/^#([0-9a-f]{3,8})$/i);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = h.split('').map((c) => c + c).join('');
    const n = parseInt(h.slice(0, 6), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  m = s.match(/^rgba?\(([^)]+)\)$/i);
  if (m) {
    const parts = m[1].split(/[\s,/]+/).filter(Boolean).map(parseFloat);
    if (parts.length >= 3) return [parts[0] / 255, parts[1] / 255, parts[2] / 255];
  }
  // Anything exotic (color-mix, oklch...) — let the browser resolve it.
  try {
    const probe = document.createElement('span');
    probe.style.color = s;
    probe.style.display = 'none';
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color;
    probe.remove();
    if (resolved && resolved !== s) return parseColor(resolved, fallback);
  } catch { /* headless / detached document */ }
  return fallback;
}

const mixc = (a, b, t) => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];

/* --- state targets ------------------------------------------------------
   The whole personality of the being is this table. Every number is a target
   that a spring or an exponential chases, so states blend instead of cutting.
   ------------------------------------------------------------------------ */

const S = {
  //            core  iris  spread open  spin  think speak act   hazard motes flow  breath
  idle:      { core: 0.215, iris: 0.44, spread: 1.00, open: 0.10, spin: 0.10, think: 0.06, speak: 0.00, act: 0.0, hazard: 0.0, motes: 0.30, flow: 0.15, breath: 1.00, gain: 0.86 },
  listening: { core: 0.235, iris: 0.88, spread: 1.15, open: 1.00, spin: 0.34, think: 0.16, speak: 0.00, act: 0.0, hazard: 0.0, motes: 1.00, flow: -1.0, breath: 0.28, gain: 1.05 },
  thinking:  { core: 0.150, iris: 0.13, spread: 0.90, open: 0.16, spin: 1.00, think: 1.00, speak: 0.00, act: 0.0, hazard: 0.0, motes: 0.55, flow: 0.05, breath: 0.10, gain: 1.00 },
  speaking:  { core: 0.230, iris: 0.60, spread: 1.06, open: 0.42, spin: 0.42, think: 0.22, speak: 1.00, act: 0.0, hazard: 0.0, motes: 0.62, flow: 1.00, breath: 0.00, gain: 1.12 },
  acting:    { core: 0.195, iris: 0.30, spread: 1.02, open: 0.24, spin: 0.55, think: 0.30, speak: 0.00, act: 1.0, hazard: 0.22, motes: 0.40, flow: 0.35, breath: 0.18, gain: 0.98 },
  alert:     { core: 0.225, iris: 0.96, spread: 1.10, open: 0.70, spin: 0.00, think: 0.35, speak: 0.00, act: 0.3, hazard: 1.0, motes: 0.34, flow: 0.0, breath: 0.00, gain: 1.20 },
};

/* Ring rotation speeds (rev/s at spin=1). Sign alternates so the assembly
   counter-rotates — that is what makes it read as machined, not as a spinner. */
const RING_SPEED = [0.055, -0.032, 0.018, -0.011, 0.026, -0.007, 0.014, -0.020];
/* Detent step per ring, in revolutions, used by `acting`. */
const RING_DETENT = [1 / 24, 1 / 12, 1 / 8, 1 / 6, 1 / 12, 1 / 6, 1 / 8, 1 / 12];

const QUALITY = [
  { dpr: 1.00, bloom: false, bloomDiv: 4, filaments: 6,  motes: 28,  grain: 0.0 },
  { dpr: 1.25, bloom: true,  bloomDiv: 4, filaments: 14, motes: 90,  grain: 0.6 },
  { dpr: 1.50, bloom: true,  bloomDiv: 2, filaments: 22, motes: 170, grain: 1.0 },
  { dpr: 2.00, bloom: true,  bloomDiv: 2, filaments: 34, motes: 300, grain: 1.0 },
];

/* ==========================================================================
   SHADERS
   Everything structural is a signed distance field evaluated per pixel, so the
   being is resolution independent: the same source draws a crisp 300px corner
   widget and a 4K centre stage with no assets and no LOD.
   ========================================================================== */

/* Fullscreen triangle from gl_VertexID — no vertex buffers anywhere in here. */
const VS_FULL = `#version 300 es
void main(){
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

const GLSL_COMMON = `
#define TAU 6.283185307179586
#define PI  3.141592653589793

float h11(float p){ p = fract(p * 0.1031); p *= p + 33.33; p *= p + p; return fract(p); }
float h21(vec2 p){
  vec3 q = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
  q += dot(q, q.yzx + 33.33);
  return fract((q.x + q.y) * q.z);
}
float h2(float a, float b){ return h11(a * 57.13 + b * 131.7); }
`;

const FS_SCENE = `#version 300 es
precision highp float;
out vec4 fragColor;

uniform vec2  uRes;
uniform float uTime;
uniform vec3  uColA;      // body colour  (tier / state)
uniform vec3  uColB;      // highlight    (rim, thread heads)
uniform vec3  uColDeep;   // the dark inside the iris
uniform float uCore;      // core radius, unit space
uniform float uIris;      // aperture, 0 shut .. 1 wide
uniform float uSpread;    // ring assembly scale
uniform float uOpen;      // outward openness (listening)
uniform float uThink;
uniform float uSpeak;
uniform float uAct;
uniform float uHazard;
uniform float uIntensity;
uniform float uBreath;
uniform float uGain;      // global brightness, carries the alert strobe
uniform vec2  uLook;      // where it is attending, unit vector * strength
uniform float uCoreRot;
uniform float uIrisRot;
uniform float uSpin;
uniform float uRingRot[8];
uniform float uRingGain[8];
uniform int   uLevelSeg;  // detent ticks lit on the level ring (acting)
${GLSL_COMMON}

/* Machined ring assembly. Radius, half-width, tick count, duty cycle. */
const float RR[8] = float[8](0.285, 0.350, 0.420, 0.487, 0.575, 0.672, 0.775, 0.870);
const float RW[8] = float[8](0.0055, 0.0130, 0.0022, 0.0180, 0.0030, 0.0095, 0.0165, 0.0038);
const float RC[8] = float[8](64.0,   3.0,   1.0,   112.0,  1.0,   5.0,    8.0,   48.0);
const float RD[8] = float[8](0.34,   0.255, 1.0,   0.50,   1.0,   0.155,  0.105, 0.30);

vec4 getHex(vec2 p){
  vec4 hC = floor(vec4(p, p - vec2(0.5, 1.0)) / vec4(1.0, 1.7320508, 1.0, 1.7320508)) + 0.5;
  vec4 h  = vec4(p - hC.xy * vec2(1.0, 1.7320508), p - (hC.zw + 0.5) * vec2(1.0, 1.7320508));
  return dot(h.xy, h.xy) < dot(h.zw, h.zw) ? vec4(h.xy, hC.xy) : vec4(h.zw, hC.zw + 0.5);
}
float hexD(vec2 p){ p = abs(p); return max(dot(p, vec2(0.5, 0.8660254)), p.x); }

float sdHex(vec2 p, float r){
  const vec3 k = vec3(-0.8660254, 0.5, 0.5773503);
  p = abs(p);
  p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;
  p -= vec2(clamp(p.x, -k.z * r, k.z * r), r);
  return length(p) * sign(p.y);
}

/* Anti-aliased fill of a signed distance, one pixel wide, at any resolution. */
float band(float d){ float w = fwidth(d) + 1e-7; return smoothstep(w, -w, d); }

/* Periodic segment mask centred on 0 so the fract() seam always lands in a gap. */
float arcMask(float ang, float count, float duty, float rot){
  float t = fract((ang / TAU + rot) * count + 0.5) - 0.5;
  float sd = abs(t) - duty * 0.5;
  float fw = clamp(fwidth(t), 1e-4, 0.25);
  return smoothstep(fw, -fw, sd);
}

void main(){
  vec2 res = uRes;
  float m = min(res.x, res.y);
  vec2 uv = (gl_FragCoord.xy - 0.5 * res) / m * 2.0;

  float breathe = 1.0 + 0.030 * uBreath * sin(uTime * 1.45)
                      + 0.020 * uIntensity * uSpeak;
  float sp = uSpread * breathe;

  vec3 col = vec3(0.0);
  float r0 = length(uv);
  float a0 = atan(uv.y, uv.x);

  /* -- deep field: the being sits in its own light ---------------------- */
  col += uColA * exp(-r0 * 2.6) * 0.045 * uGain;
  col += uColA * exp(-abs(r0 - uCore * 1.35) * 9.0) * 0.05 * (0.5 + uOpen);

  /* -- ring assembly ---------------------------------------------------- */
  for (int i = 0; i < 8; i++) {
    float R = RR[i] * sp;
    float d = abs(r0 - R) - RW[i];
    float mask = band(d);
    if (mask < 0.002) continue;
    float am = (RD[i] > 0.99) ? 1.0 : arcMask(a0, RC[i], RD[i], uRingRot[i]);
    float g = uRingGain[i] * mask * am;
    /* the heavy rings pick up a machined top-light so they read as metal */
    float shade = 0.72 + 0.45 * cos(a0 - 1.9);
    col += mix(uColA, uColB, 0.30) * g * shade;
  }

  /* bolts on the outer bezel — small, deliberate, mechanical */
  {
    float R = RR[6] * sp;
    float t = fract((a0 / TAU + uRingRot[6]) * 8.0 + 0.5) - 0.5;
    vec2 bp = vec2(t * TAU / 8.0 * R, r0 - R);
    col += mix(uColB, vec3(1.0), 0.3) * band(length(bp) - 0.0055) * 0.75 * uRingGain[6];
  }

  /* level ring — the being's own VU. Continuous when listening/speaking,
     stepped into detents when acting, so "working" never looks like "hearing". */
  {
    float R = RR[4] * sp;
    float d = abs(r0 - R) - 0.0075;
    float sweep = clamp(uIntensity, 0.0, 1.0) * 0.82;
    float ang = fract((a0 + PI * 0.5) / TAU);
    float stepped = float(uLevelSeg) / 24.0;
    float lvl = mix(sweep, stepped, uAct);
    float fw = clamp(fwidth(ang), 1e-4, 0.2);
    float on = smoothstep(lvl + fw, lvl - fw, ang);
    float ticks = mix(1.0, arcMask(a0, 24.0, 0.55, uRingRot[4]), uAct);
    col += mix(uColB, vec3(1.0), 0.25) * band(d) * on * ticks * (0.6 + 0.9 * uIntensity);
    col += uColA * band(abs(r0 - R) - 0.0018) * 0.25;
  }

  /* index marker — a chevron that points where A.R.S is attending. This is the
     single cheapest cue that makes it feel aware rather than decorative. */
  {
    float la = atan(uLook.y, uLook.x);
    float R = RR[2] * sp;
    float t = fract((a0 - la) / TAU + 0.5) - 0.5;
    float w = 0.055;
    float taper = 1.0 - smoothstep(0.0, w, abs(t));
    float d = abs(r0 - R - taper * 0.018) - 0.0035 * taper;
    col += mix(uColB, vec3(1.0), 0.35) * band(d) * taper * (0.55 + 0.6 * length(uLook));
  }

  /* -- containment shell: a hard hexagon that appears when it opens up --- */
  {
    float amt = max(uOpen * 0.55, uHazard);
    if (amt > 0.01) {
      float hs = sdHex(uv * mat2(cos(0.26), -sin(0.26), sin(0.26), cos(0.26)), 0.955 * sp);
      float dash = arcMask(a0, 36.0, 0.55, uTime * 0.01 * uSpin);
      col += uColA * band(abs(hs) - 0.0022) * dash * amt * 0.85;
      col += uColA * exp(-abs(hs) * 40.0) * amt * 0.05;
    }
  }

  /* -- listening: wavefronts travelling INWARD, it is taking something in - */
  if (uOpen > 0.01) {
    float wf = fract(r0 * 3.4 + uTime * 0.55);
    float ring = smoothstep(0.86, 1.0, wf) * smoothstep(1.02 * sp, 0.30, r0);
    col += uColA * ring * uOpen * 0.10 * (0.4 + uIntensity);
  }

  /* -- the crystalline core -------------------------------------------- */
  vec2 q = uv - uLook * 0.014;
  float rq = length(q);
  float aq = atan(q.y, q.x);
  float cr = uCore * breathe;

  float k = 6.0;
  float kseg = TAU / k;
  float af = mod(aq + uCoreRot, kseg) - kseg * 0.5;
  vec2 fq = vec2(cos(af), sin(af)) * rq;

  float coreMask = smoothstep(cr, cr * 0.80, rq);
  if (coreMask > 0.001) {
    vec2 lp = fq * (7.2 / max(cr, 0.06));
    vec4 hx = getHex(lp + vec2(uTime * 0.04, 0.0));
    float ed = 0.5 - hexD(hx.xy);
    float cid = h21(hx.zw);
    /* activity propagates outward from the centre — a thought crossing it */
    float wave = sin(rq * 30.0 - uTime * (1.5 + 5.0 * uThink) + cid * TAU);
    float act = smoothstep(0.15, 0.95, wave) * (0.20 + 0.80 * uThink) * (0.5 + 0.9 * cid);
    float wire = smoothstep(0.075, 0.008, ed);
    float fill = smoothstep(0.34, 0.03, ed);
    float lattice = wire * (0.45 + 1.15 * act) + fill * act * 0.40;
    col += mix(uColA, uColB, clamp(act, 0.0, 1.0) * 0.75) * lattice * coreMask * 1.25;
    /* faceting: the kaleidoscope seams catch the light like cut crystal */
    float facet = pow(abs(cos(af * 3.0)), 8.0);
    col += uColB * facet * coreMask * 0.10;
  }

  /* core shell + halo */
  col += mix(uColB, vec3(1.0), 0.30) * band(abs(rq - cr) - 0.0035) * 1.35;
  col += uColA * exp(-max(rq - cr, 0.0) * 13.0) * 0.20 * uGain;

  /* -- the iris: eight machined blades over the lattice ----------------- */
  float apr = cr * (0.10 + 0.74 * uIris);
  float bseg = TAU / 8.0;
  float ba = mod(aq + uIrisRot, bseg) - bseg * 0.5;
  float bladeR = rq * cos(ba);
  float apert = bladeR - apr;
  float insideAp = band(apert);
  float bladeZone = coreMask * (1.0 - insideAp);
  col = mix(col, col * 0.30 + uColDeep * 0.55, bladeZone * 0.72);
  float seam = band(abs(abs(ba) - bseg * 0.5) * rq - 0.0016);
  col += uColB * seam * bladeZone * 0.45;
  col += mix(uColB, vec3(1.0), 0.45) * band(abs(apert) - 0.0030) * 1.5;

  /* nucleus — the light that lives behind the aperture */
  float nu = exp(-pow(rq / max(apr, 0.02), 2.0) * 2.0);
  col += mix(uColB, vec3(1.0), 0.55) * nu * insideAp * (0.75 + 1.5 * uIntensity);
  col += vec3(1.0) * exp(-rq * rq / (0.0006 + 0.0055 * (1.0 - uThink))) * (0.35 + 0.85 * uThink);

  /* -- speaking: radial petals driven by intensity ---------------------- */
  if (uSpeak > 0.01) {
    float spec = 0.0;
    for (int i = 1; i <= 4; i++) {
      float fi = float(i);
      spec += sin(aq * (fi * 3.0 + 1.0) + uTime * (2.2 + fi * 1.9) + fi * 1.7) / fi;
    }
    spec = spec * 0.42 + 0.5;
    float pl = cr + 0.018 + (0.020 + 0.20 * uIntensity) * spec;
    col += mix(uColA, uColB, 0.55) * band(abs(rq - pl) - 0.0032) * uSpeak * (0.6 + 1.1 * uIntensity);
    col += uColA * smoothstep(pl, cr, rq) * uSpeak * 0.10 * uIntensity;
  }

  /* -- acting: actuator ticks that extend and retract -------------------- */
  if (uAct > 0.01) {
    float R = RR[5] * sp;
    float ext = 0.012 + 0.020 * (0.5 + 0.5 * sin(uTime * 3.2));
    float t = fract((a0 / TAU + uRingRot[5]) * 12.0 + 0.5) - 0.5;
    float w = clamp(fwidth(t), 1e-4, 0.25);
    float tick = smoothstep(w, -w, abs(t) - 0.035);
    float d = abs(r0 - (R + ext * 0.5)) - ext * 0.5;
    col += mix(uColA, uColB, 0.5) * band(d) * tick * uAct * 0.7;
  }

  /* -- alert: hazard chevrons, containment, scan bar --------------------- */
  if (uHazard > 0.01) {
    float R = 0.925 * sp;
    float d = abs(r0 - R) - 0.024;
    float chev = step(0.5, fract((a0 / TAU) * 22.0 + (r0 - R) * 7.0 + uTime * 0.08));
    col += uColA * band(d) * chev * uHazard * 0.95;
    float bar = exp(-pow((uv.y - (fract(uTime * 0.42) * 2.6 - 1.3)) / 0.028, 2.0));
    col += uColA * bar * uHazard * 0.22;
    col += uColA * band(abs(r0 - RR[7] * sp) - 0.0018) * uHazard * 0.5;
  }

  fragColor = vec4(col * uGain, 1.0);
}`;

/* -- filaments: instanced quadratic strands with a travelling head ------- */
const VS_FILAMENT = `#version 300 es
precision highp float;
uniform float uTime, uCore, uSpread, uThreads, uWidth, uActive;
uniform vec2  uScale;
out float vG;
out float vSide;
${GLSL_COMMON}
const int SEG = 20;
void main(){
  int vid = gl_VertexID;
  float s = float(vid >> 1) / float(SEG);
  float side = float(vid & 1) * 2.0 - 1.0;
  float fi = float(gl_InstanceID);
  float seed = fi * 1.6180339887;
  float alive = step(fi, uActive - 0.5);

  float rate = 0.30 + 1.75 * uThreads;
  float cyc  = uTime * rate * (0.55 + 0.9 * h11(seed + 9.1)) + h11(seed) * 13.0;
  float life = fract(cyc);
  float ep   = floor(cyc);

  float a0 = h2(seed, ep) * TAU;
  float a1 = a0 + (h2(seed + 1.3, ep) - 0.5) * 2.7;
  float r0 = uCore * (0.80 + 0.22 * h2(seed + 2.7, ep));
  float r1 = uCore + (0.09 + 0.60 * h2(seed + 3.9, ep)) * uSpread;

  vec2 P0 = vec2(cos(a0), sin(a0)) * r0;
  vec2 P2 = vec2(cos(a1), sin(a1)) * r1;
  vec2 dir = P2 - P0;
  vec2 nrm = normalize(vec2(-dir.y, dir.x));
  vec2 P1 = mix(P0, P2, 0.5) + nrm * (h2(seed + 5.1, ep) - 0.5) * 0.75 * length(dir);

  float t = s;
  vec2 pos = (1.0 - t) * (1.0 - t) * P0 + 2.0 * (1.0 - t) * t * P1 + t * t * P2;
  vec2 tg  = 2.0 * (1.0 - t) * (P1 - P0) + 2.0 * t * (P2 - P1);
  vec2 n   = normalize(vec2(-tg.y, tg.x));

  float head = life * 1.28;
  float g = exp(-max(head - s, 0.0) * 6.5) * step(s, head);
  float env = smoothstep(0.0, 0.05, life) * smoothstep(1.0, 0.70, life);

  vG = g * env * alive;
  vSide = side;
  pos += n * side * uWidth * (0.30 + 1.0 * g) * mix(0.5, 1.0, env);
  gl_Position = vec4(pos * uScale, 0.0, 1.0);
}`;

const FS_FILAMENT = `#version 300 es
precision highp float;
in float vG;
in float vSide;
uniform vec3 uColA, uColB;
out vec4 fragColor;
void main(){
  float e = 1.0 - abs(vSide);
  float a = vG * e * e;
  vec3 c = mix(uColA, mix(uColB, vec3(1.0), 0.5), clamp(vG, 0.0, 1.0));
  fragColor = vec4(c * a * 1.5, 1.0);
}`;

/* -- motes: dust the being inhales when listening, exhales when speaking -- */
const VS_MOTE = `#version 300 es
precision highp float;
uniform float uTime, uCore, uSpread, uFlow, uActive, uSpin, uSizeScale;
uniform vec2  uScale;
out vec2 vUV;
out float vA;
${GLSL_COMMON}
void main(){
  vec2 corner = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1)) * 2.0 - 1.0;
  float fi = float(gl_InstanceID);
  float seed = fi * 0.7548776662;
  float alive = step(fi, uActive - 0.5);

  float ang0 = h11(seed) * TAU;
  float spd  = (0.05 + 0.30 * h11(seed + 2.0)) * (h11(seed + 5.0) < 0.5 ? -1.0 : 1.0);
  float rBase = uCore * 1.20 + h11(seed + 3.0) * 0.66 * uSpread;

  float f = clamp(abs(uFlow), 0.0, 1.0);
  float ph = fract(uTime * (0.10 + 0.26 * h11(seed + 7.0)) * (0.4 + f) + h11(seed + 11.0));
  float travel = uFlow < 0.0 ? (1.0 - ph) : ph;
  float rFlow = mix(uCore * 1.02, rBase, travel);
  float r = mix(rBase, rFlow, f);

  float ang = ang0 + uTime * spd * (0.35 + uSpin);
  vec2 pos = vec2(cos(ang), sin(ang)) * r;

  float size = (0.0018 + 0.0042 * h11(seed + 13.0)) * uSizeScale;
  pos += corner * size;

  float fade = mix(1.0, sin(ph * PI), f);
  vUV = corner;
  vA = alive * fade * (0.35 + 0.65 * h11(seed + 17.0));
  gl_Position = vec4(pos * uScale, 0.0, 1.0);
}`;

const FS_MOTE = `#version 300 es
precision highp float;
in vec2 vUV;
in float vA;
uniform vec3 uColA, uColB;
out vec4 fragColor;
void main(){
  float d = dot(vUV, vUV);
  float a = exp(-d * 3.4) * vA;
  fragColor = vec4(mix(uColA, uColB, 0.5) * a * 0.9, 1.0);
}`;

/* -- bloom: bright pass, separable blur, composite ---------------------- */
const FS_BRIGHT = `#version 300 es
precision highp float;
uniform sampler2D uTex;
uniform vec2 uTexel;
uniform float uThresh;
out vec4 fragColor;
void main(){
  vec2 uv = gl_FragCoord.xy * uTexel;
  vec3 c = texture(uTex, uv + vec2( 0.5,  0.5) * uTexel).rgb
         + texture(uTex, uv + vec2(-0.5,  0.5) * uTexel).rgb
         + texture(uTex, uv + vec2( 0.5, -0.5) * uTexel).rgb
         + texture(uTex, uv + vec2(-0.5, -0.5) * uTexel).rgb;
  c *= 0.25;
  float l = max(max(c.r, c.g), c.b);
  fragColor = vec4(c * smoothstep(uThresh, uThresh + 0.35, l), 1.0);
}`;

const FS_BLUR = `#version 300 es
precision highp float;
uniform sampler2D uTex;
uniform vec2 uTexel;
uniform vec2 uDir;
out vec4 fragColor;
void main(){
  vec2 uv = gl_FragCoord.xy * uTexel;
  vec2 o1 = uDir * uTexel * 1.3846153846;
  vec2 o2 = uDir * uTexel * 3.2307692308;
  vec3 c = texture(uTex, uv).rgb * 0.2270270270;
  c += (texture(uTex, uv + o1).rgb + texture(uTex, uv - o1).rgb) * 0.3162162162;
  c += (texture(uTex, uv + o2).rgb + texture(uTex, uv - o2).rgb) * 0.0702702703;
  fragColor = vec4(c, 1.0);
}`;

const FS_COMPOSITE = `#version 300 es
precision highp float;
uniform sampler2D uScene;
uniform sampler2D uBloom;
uniform vec2  uRes;
uniform float uBloomAmt;
uniform float uGrain;
uniform float uScan;
uniform float uChroma;
uniform float uTear;
uniform float uTime;
out vec4 fragColor;
${GLSL_COMMON}
void main(){
  vec2 uv = gl_FragCoord.xy / uRes;

  /* horizontal tear — only ever fires on alert, and only on a few scanlines */
  float bandY = floor(uv.y * 90.0);
  float glitch = step(0.86, h2(bandY, floor(uTime * 11.0))) * uTear;
  vec2 tuv = uv + vec2((h2(bandY, floor(uTime * 11.0) + 3.0) - 0.5) * 0.06 * glitch, 0.0);

  vec3 c;
  if (uChroma > 0.0005) {
    vec2 d = (tuv - 0.5) * uChroma;
    c = vec3(texture(uScene, tuv + d).r, texture(uScene, tuv).g, texture(uScene, tuv - d).b);
  } else {
    c = texture(uScene, tuv).rgb;
  }
  c += texture(uBloom, tuv).rgb * uBloomAmt;

  /* CRT scanlines, inside the being only — it is a projection, not a sticker */
  float sl = 1.0 - uScan * (0.5 + 0.5 * sin(gl_FragCoord.y * 3.14159));
  c *= sl;

  /* fine grain keeps large flat glows from banding on 8-bit displays */
  if (uGrain > 0.0005) {
    float n = h21(gl_FragCoord.xy + fract(uTime) * 512.0) - 0.5;
    c += n * uGrain;
  }

  c = max(c, vec3(0.0));
  /* soft filmic shoulder so the nucleus blooms out instead of clipping flat */
  c = c / (1.0 + c * 0.42);
  float a = clamp(max(max(c.r, c.g), c.b) * 1.85, 0.0, 1.0);
  fragColor = vec4(c, a);
}`;
