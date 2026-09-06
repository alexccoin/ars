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

/** Push a colour away from its own luminance. The token ramp is tuned for text
 *  on a dark surface, so it is deliberately pale; additive rendering plus bloom
 *  then washes it to white and every tier looks the same. This buys the hue
 *  back without touching the tokens. */
function saturate(c, k) {
  const l = c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
  return [clamp(l + (c[0] - l) * k, 0, 1), clamp(l + (c[1] - l) * k, 0, 1), clamp(l + (c[2] - l) * k, 0, 1)];
}

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
uniform float uMinW;      // half-width floor, in unit space, = ~0.6px at this size
uniform float uDetail;    // 0 = 44px favicon, 1 = full centre-stage assembly
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

/* NOTE — every smoothstep in this file is written edge0 < edge1 and inverted
   by hand where a falling ramp is wanted. GLSL leaves smoothstep(a, b, x)
   UNDEFINED for a >= b, and it is not a theoretical worry: written the
   reversed way, the speaking petals' inner fill painted two grey wedges from
   the core to the edge of the frame on ANGLE. Do not "simplify" these back.

   Anti-aliased fill of a signed distance, one pixel wide, at any resolution.
   The clamp is not cosmetic: for a distance that depends on the ANGLE (the
   index chevron, the speaking petals) fwidth() explodes near the centre, where
   one pixel spans a large arc. Unclamped, smoothstep(w,-w,d) then returns ~0.5
   across the whole sector and a hairline becomes a 20-degree wedge of fog
   reaching off-screen. uMinW is the known half-pixel in unit space, so the
   filter is allowed to be between half a pixel and four pixels wide, never
   more. */
float band(float d){
  float w = clamp(fwidth(d), uMinW * 0.5, uMinW * 4.0) + 1e-7;
  return 1.0 - smoothstep(-w, w, d);
}

/* Periodic segment mask centred on 0 so the fract() seam always lands in a gap. */
float arcMask(float ang, float count, float duty, float rot){
  float t = fract((ang / TAU + rot) * count + 0.5) - 0.5;
  float sd = abs(t) - duty * 0.5;
  float fw = clamp(fwidth(t), 1e-4, 0.08);
  return 1.0 - smoothstep(-fw, fw, sd);
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
    if (uRingGain[i] < 0.004) continue;
    float R = RR[i] * sp;
    float d = abs(r0 - R) - max(RW[i], uMinW);
    float mask = band(d);
    if (mask < 0.002) continue;
    /* thin the tick counts down as the being shrinks, or 112 ticks become mush */
    float cnt = RC[i] > 8.0 ? max(8.0, floor(RC[i] * mix(0.22, 1.0, uDetail))) : RC[i];
    float am = (RD[i] > 0.99) ? 1.0 : arcMask(a0, cnt, RD[i], uRingRot[i]);
    float g = uRingGain[i] * mask * am;
    /* the heavy rings pick up a machined top-light so they read as metal */
    float shade = 0.72 + 0.45 * cos(a0 - 1.9);
    col += mix(uColA, uColB, 0.30) * g * shade * 0.78;
  }

  /* bolts on the outer bezel — small, deliberate, mechanical */
  {
    float R = RR[6] * sp;
    float t = fract((a0 / TAU + uRingRot[6]) * 8.0 + 0.5) - 0.5;
    vec2 bp = vec2(t * TAU / 8.0 * R, r0 - R);
    col += mix(uColB, vec3(1.0), 0.3) * band(length(bp) - max(0.0055, uMinW * 1.6)) * 0.75 * uRingGain[6] * uDetail;
  }

  /* level ring — the being's own VU. Continuous when listening/speaking,
     stepped into detents when acting, so "working" never looks like "hearing". */
  {
    float R = RR[4] * sp;
    float d = abs(r0 - R) - max(0.0075, uMinW);
    float sweep = clamp(uIntensity, 0.0, 1.0) * 0.82;
    float ang = fract((a0 + PI * 0.5) / TAU);
    float stepped = float(uLevelSeg) / 24.0;
    float lvl = mix(sweep, stepped, uAct);
    float fw = clamp(fwidth(ang), 1e-4, 0.2);
    float on = 1.0 - smoothstep(lvl - fw, lvl + fw, ang);
    float ticks = mix(1.0, arcMask(a0, 24.0, 0.55, uRingRot[4]), uAct);
    col += mix(uColB, vec3(1.0), 0.10) * band(d) * on * ticks * (0.6 + 0.9 * uIntensity);
    col += uColA * band(abs(r0 - R) - max(0.0018, uMinW * 0.8)) * 0.25;
  }

  /* index marker — a chevron that points where A.R.S is attending. This is the
     single cheapest cue that makes it feel aware rather than decorative. */
  {
    /* uLook is a direction times a strength, so it is (0,0) until something
       has asked for attention — and atan(0,0) is undefined in GLSL. On this
       driver it returns NaN, the NaN reaches taper, and the chevron paints a
       drifting grey wedge across the whole frame. Bias x so the angle is
       always defined; strength is applied separately below. */
    float la = atan(uLook.y, uLook.x + 1e-5);
    float R = RR[2] * sp;
    float t = fract((a0 - la) / TAU + 0.5) - 0.5;
    float w = 0.055;
    float taper = 1.0 - smoothstep(0.0, w, abs(t));
    float d = abs(r0 - R - taper * 0.018) - max(0.0035, uMinW) * taper;
    col += mix(uColB, vec3(1.0), 0.35) * band(d) * taper * (0.55 + 0.6 * length(uLook));
  }

  /* -- containment shell: a hard hexagon that appears when it opens up --- */
  {
    float amt = max(uOpen * 0.55, uHazard);
    if (amt > 0.01) {
      float hs = sdHex(uv * mat2(cos(0.26), -sin(0.26), sin(0.26), cos(0.26)), 0.955 * sp);
      float dash = arcMask(a0, mix(12.0, 36.0, uDetail), 0.55, uTime * 0.01 * uSpin);
      col += uColA * band(abs(hs) - max(0.0022, uMinW)) * dash * amt * 0.85;
      col += uColA * exp(-abs(hs) * 40.0) * amt * 0.05;
    }
  }

  /* -- listening: wavefronts travelling INWARD, it is taking something in - */
  if (uOpen > 0.01) {
    float wf = fract(r0 * 3.4 + uTime * 0.55);
    float ring = smoothstep(0.86, 1.0, wf) * (1.0 - smoothstep(0.30, 1.02 * sp, r0));
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

  float coreMask = 1.0 - smoothstep(cr * 0.80, cr, rq);
  if (coreMask > 0.001) {
    vec2 lp = fq * (mix(3.0, 7.2, uDetail) / max(cr, 0.06));
    vec4 hx = getHex(lp + vec2(uTime * 0.04, 0.0));
    float ed = 0.5 - hexD(hx.xy);
    float cid = h21(hx.zw);
    /* activity propagates outward from the centre — a thought crossing it */
    float wave = sin(rq * 30.0 - uTime * (1.5 + 5.0 * uThink) + cid * TAU);
    float act = smoothstep(0.15, 0.95, wave) * (0.20 + 0.80 * uThink) * (0.5 + 0.9 * cid);
    float wire = 1.0 - smoothstep(0.008, 0.075, ed);
    float fill = 1.0 - smoothstep(0.03, 0.34, ed);
    float lattice = wire * (0.45 + 1.15 * act) + fill * act * 0.40;
    col += mix(uColA, uColB, clamp(act, 0.0, 1.0) * 0.75) * lattice * coreMask * 1.25;
    /* faceting: the kaleidoscope seams catch the light like cut crystal */
    float facet = pow(abs(cos(af * 3.0)), 8.0);
    col += uColB * facet * coreMask * 0.10;
  }

  /* core shell + halo */
  col += mix(uColB, vec3(1.0), 0.12) * band(abs(rq - cr) - max(0.0035, uMinW)) * 1.15;
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
  float seam = band(abs(abs(ba) - bseg * 0.5) * rq - max(0.0016, uMinW * 0.7));
  col += uColB * seam * bladeZone * 0.45;
  col += mix(uColB, vec3(1.0), 0.25) * band(abs(apert) - max(0.0030, uMinW)) * 1.25;

  /* nucleus — the light that lives behind the aperture */
  float nu = exp(-pow(rq / max(apr, 0.02), 2.0) * 3.4);
  col += mix(uColB, vec3(1.0), 0.22) * nu * insideAp * (0.55 + 1.15 * uIntensity);
  col += vec3(1.0) * exp(-rq * rq / (0.0006 + 0.0055 * (1.0 - uThink))) * (0.35 + 0.85 * uThink);

  /* -- speaking: radial petals driven by intensity ---------------------- */
  if (uSpeak > 0.01) {
    float spec = 0.0;
    for (int i = 1; i <= 4; i++) {
      float fi = float(i);
      spec += sin(aq * (fi * 3.0 + 1.0) + uTime * (2.2 + fi * 1.9) + fi * 1.7) / fi;
    }
    /* The four harmonics sum to as low as -2.08, which put the petal radius
       INSIDE the core and silently reversed the smoothstep below — two grey
       wedges across the frame, only while speaking, only at some phases. Rectify
       the waveform instead: a VU that bottoms out reads better anyway. */
    spec = clamp(spec * 0.42 + 0.5, 0.0, 1.4);
    float pl = cr + 0.018 + (0.020 + 0.20 * uIntensity) * spec;
    col += mix(uColA, uColB, 0.55) * band(abs(rq - pl) - max(0.0032, uMinW)) * uSpeak * (0.6 + 1.1 * uIntensity);
    col += uColA * (1.0 - smoothstep(cr, pl, rq)) * uSpeak * 0.10 * uIntensity;
  }

  /* -- acting: actuator ticks that extend and retract -------------------- */
  if (uAct > 0.01) {
    float R = RR[5] * sp;
    float ext = 0.012 + 0.020 * (0.5 + 0.5 * sin(uTime * 3.2));
    float t = fract((a0 / TAU + uRingRot[5]) * 12.0 + 0.5) - 0.5;
    float w = clamp(fwidth(t), 1e-4, 0.25);
    float tick = 1.0 - smoothstep(-w, w, abs(t) - 0.035);
    float d = abs(r0 - (R + ext * 0.5)) - ext * 0.5;
    col += mix(uColA, uColB, 0.5) * band(d) * tick * uAct * 0.7;
  }

  /* -- alert: hazard chevrons, containment, scan bar --------------------- */
  if (uHazard > 0.01) {
    float R = 0.925 * sp;
    float d = abs(r0 - R) - max(0.024, uMinW * 3.0);
    float chev = step(0.5, fract((a0 / TAU) * mix(8.0, 22.0, uDetail) + (r0 - R) * 7.0 + uTime * 0.08));
    col += uColA * band(d) * chev * uHazard * 0.95;
    float bar = exp(-pow((uv.y - (fract(uTime * 0.42) * 2.6 - 1.3)) / 0.028, 2.0));
    col += uColA * bar * uHazard * 0.22;
    col += uColA * band(abs(r0 - RR[7] * sp) - max(0.0018, uMinW * 0.8)) * uHazard * 0.5;
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
  float r1 = uCore + (0.07 + 0.42 * h2(seed + 3.9, ep)) * uSpread;

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
  float env = smoothstep(0.0, 0.05, life) * (1.0 - smoothstep(0.70, 1.0, life));

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
  vec3 c = mix(uColA, mix(uColB, vec3(1.0), 0.25), clamp(vG, 0.0, 1.0));
  fragColor = vec4(c * a * 1.8, 1.0);
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

/* ==========================================================================
   GL PLUMBING
   Deliberately tiny. Every helper here exists because the alternative was
   repeating the same six lines in five places, not because it is a framework.
   ========================================================================== */

function compileShader(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh) || 'unknown';
    gl.deleteShader(sh);
    throw new Error(`shader compile failed: ${log}`);
  }
  return sh;
}

/** Link a program and pre-resolve every active uniform into a plain object. */
function makeProgram(gl, vsSrc, fsSrc) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(prog) || 'unknown';
    gl.deleteProgram(prog);
    throw new Error(`program link failed: ${log}`);
  }
  const u = Object.create(null);
  const n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < n; i++) {
    const info = gl.getActiveUniform(prog, i);
    if (!info) continue;
    const name = info.name.replace(/\[0\]$/, '');
    u[name] = gl.getUniformLocation(prog, info.name);
  }
  return { prog, u };
}

/** A colour render target. RGBA16F when the driver will render to it (so the
 *  nucleus can exceed 1.0 and actually bloom), RGBA8 otherwise. */
class Target {
  constructor(gl, hdr) {
    this.gl = gl;
    this.hdr = hdr;
    this.w = 0; this.h = 0;
    this.tex = gl.createTexture();
    this.fbo = gl.createFramebuffer();
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }
  size(w, h) {
    w = Math.max(1, w | 0); h = Math.max(1, h | 0);
    if (w === this.w && h === this.h) return;
    const gl = this.gl;
    this.w = w; this.h = h;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    if (this.hdr) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
    else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
  }
  bind() {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.viewport(0, 0, this.w, this.h);
  }
  destroy() {
    this.gl.deleteTexture(this.tex);
    this.gl.deleteFramebuffer(this.fbo);
  }
}

/* ==========================================================================
   THEME
   Everything the being is coloured with comes out of theme.css. Read once at
   construction, again on refreshTheme(), and automatically when the OS flips
   between light and dark.
   ========================================================================== */

const FALLBACK_THEME = {
  base: [0.133, 0.827, 0.941],
  rim: [0.714, 0.957, 1.0],
  deep: [0.016, 0.102, 0.149],
  states: {
    idle: [0.059, 0.663, 0.784], listening: [0.369, 0.902, 1.0], thinking: [0.133, 0.827, 0.941],
    speaking: [0.525, 0.929, 1.0], acting: [1.0, 0.698, 0.247], alert: [1.0, 0.361, 0.42],
  },
  tiers: {
    recall: [0.624, 0.933, 1.0], documents: [0.247, 0.863, 0.627],
    'local model': [0.690, 0.549, 1.0], 'cloud model': [1.0, 0.698, 0.247],
  },
  bloom: 1.0, grain: 0.035, scan: 0.055,
};

const tierSlug = (tier) => String(tier || '').trim().toLowerCase().replace(/\s+/g, '-');

function readTheme(host) {
  const cs = getComputedStyle(host);
  const v = (name) => cs.getPropertyValue(name);
  const col = (name, fb) => parseColor(v(name), fb);
  const num = (name, fb) => {
    const n = parseFloat(v(name));
    return Number.isFinite(n) ? n : fb;
  };
  const theme = {
    base: col('--ars-entity-base', FALLBACK_THEME.base),
    rim: col('--ars-entity-rim', FALLBACK_THEME.rim),
    deep: col('--ars-entity-deep', FALLBACK_THEME.deep),
    states: {}, tiers: {},
    bloom: num('--ars-entity-bloom', FALLBACK_THEME.bloom),
    grain: num('--ars-entity-grain', FALLBACK_THEME.grain),
    scan: num('--ars-entity-scanline', FALLBACK_THEME.scan),
  };
  for (const s of ARS_STATES) theme.states[s] = col(`--ars-entity-state-${s}`, FALLBACK_THEME.states[s]);
  for (const t of ARS_TIERS) theme.tiers[t] = col(`--ars-entity-tier-${tierSlug(t)}`, FALLBACK_THEME.tiers[t]);
  return theme;
}

/* Agent states that are not one of the six get mapped, never dropped. app.js
   sends protocol AgentState values, which include a few the being has no
   distinct pose for. */
const STATE_ALIAS = {
  idle: 'idle', ready: 'idle', connecting: 'idle',
  listening: 'listening', hearing: 'listening', wake: 'listening',
  thinking: 'thinking', planning: 'thinking', retrieving: 'thinking',
  speaking: 'speaking', responding: 'speaking',
  acting: 'acting', tool_call: 'acting', executing: 'acting',
  waiting_for_consent: 'acting',
  alert: 'alert', error: 'alert', denied: 'alert', blocked: 'alert',
};

/* ==========================================================================
   THE ANIMATION MODEL
   Renderer-independent: it produces one flat bag of numbers per frame, which
   either the WebGL2 renderer or the Canvas2D renderer draws. That is why the
   fallback can never drift out of sync with the real thing.
   ========================================================================== */

const KEY_TAU = {
  core: 0.16, iris: 0.18, spin: 0.42, think: 0.28, speak: 0.14,
  act: 0.22, hazard: 0.09, motes: 0.55, flow: 0.40, breath: 0.60, gain: 0.22,
};

class Model {
  constructor(theme) {
    this.theme = theme;
    this.state = 'idle';
    this.tier = null;
    this.t = 0;
    this.v = { ...S.idle };
    this.spread = new Spring(S.idle.spread, 90, 14);
    this.open = new Spring(S.idle.open, 110, 15);
    this.intensity = 0;
    this.intensityTarget = 0;
    this.impulse = 0;
    this.coreRot = 0;
    this.irisRot = 0;
    this.ringCont = new Float32Array(8);
    this.ringSpring = RING_SPEED.map(() => new Spring(0, 200, 20));
    this.ringRot = new Float32Array(8);
    this.ringGain = new Float32Array(8).fill(1);
    /* Never exactly zero: see the atan(0,0) note in the scene shader. A small
       resting strength also gives the chevron somewhere to sit at idle. */
    this.look = [1, 0];
    this.lookTarget = [1, 0];
    this.lookStrength = 0.16;
    this.lookStrengthTarget = 0.16;
    this.colA = theme.states.idle.slice();
    this.colB = theme.rim.slice();
  }

  setState(name) {
    const key = STATE_ALIAS[name] || (ARS_STATES.includes(name) ? name : 'idle');
    if (key === this.state) return;
    this.state = key;
    this.impulse = Math.max(this.impulse, 0.55);
  }

  targetColour() {
    const th = this.theme;
    const st = th.states[this.state] || th.base;
    const tierCol = this.tier ? th.tiers[this.tier] : null;
    let a;
    if (this.state === 'alert') a = st;
    else if (this.state === 'acting') a = tierCol ? mixc(tierCol, st, 0.72) : st;
    else if (tierCol) a = mixc(tierCol, st, 0.22);
    else a = st;
    a = saturate(a, 1.35);
    /* The highlight has to stay the SAME hue, only brighter. Mixing toward
       white (or toward an ice-white rim token) is what turns a violet
       "local model" turn into generic sci-fi grey — normalise the body colour
       to full value instead, then let the rim tint it only slightly. */
    const peak = Math.max(a[0], a[1], a[2], 1e-3);
    const vivid = [a[0] / peak, a[1] / peak, a[2] / peak];
    const b = this.state === 'alert'
      ? mixc(vivid, [1, 1, 1], 0.30)
      : mixc(vivid, th.rim, 0.22);
    return [a, b];
  }

  step(dt, opts) {
    const still = !!opts.still;
    this.t += dt;
    const tgt = S[this.state] || S.idle;

    for (const k of Object.keys(KEY_TAU)) {
      this.v[k] = still ? tgt[k] : approach(this.v[k], tgt[k], KEY_TAU[k], dt);
    }
    if (still) {
      this.spread.x = tgt.spread; this.spread.v = 0;
      this.open.x = tgt.open; this.open.v = 0;
    } else {
      this.spread.target = tgt.spread; this.spread.step(dt);
      this.open.target = tgt.open; this.open.step(dt);
    }
    this.v.spread = this.spread.x;
    this.v.open = this.open.x;

    this.intensity = still
      ? this.intensityTarget
      : approach(this.intensity, this.intensityTarget, 0.075, dt);
    this.impulse = still ? 0 : approach(this.impulse, 0, 0.28, dt);

    /* rotation. spin drives everything; acting quantises it into detents with a
       spring so each step lands with a mechanical overshoot; alert locks it. */
    const spin = this.v.spin;
    const act = this.v.act;
    for (let i = 0; i < 8; i++) {
      if (!still) this.ringCont[i] += RING_SPEED[i] * spin * dt * (1 + 0.35 * this.intensity);
      const cont = this.ringCont[i];
      const det = RING_DETENT[i];
      const snapped = Math.round(cont / det) * det;
      const target = lerp(cont, snapped, act);
      if (still) { this.ringSpring[i].x = target; this.ringSpring[i].v = 0; }
      else { this.ringSpring[i].target = target; this.ringSpring[i].step(dt); }
      this.ringRot[i] = this.ringSpring[i].x;
    }
    if (!still) {
      this.coreRot += dt * (0.05 + 0.55 * this.v.think) * (this.state === 'alert' ? 0 : 1);
      this.irisRot += dt * (0.02 + 0.10 * this.v.act) * (this.state === 'alert' ? 0 : 1);
    }

    /* attention */
    const lookTau = 0.22;
    this.lookStrength = still
      ? this.lookStrengthTarget
      : approach(this.lookStrength, this.lookStrengthTarget, 0.5, dt);
    this.look[0] = still ? this.lookTarget[0] : approach(this.look[0], this.lookTarget[0], lookTau, dt);
    this.look[1] = still ? this.lookTarget[1] : approach(this.look[1], this.lookTarget[1], lookTau, dt);

    /* colour blends too — a tier change is a visible, gradual shift in hue */
    const [ta, tb] = this.targetColour();
    const ctau = 0.30;
    for (let i = 0; i < 3; i++) {
      this.colA[i] = still ? ta[i] : approach(this.colA[i], ta[i], ctau, dt);
      this.colB[i] = still ? tb[i] : approach(this.colB[i], tb[i], ctau, dt);
    }
  }

  /** Everything the renderers need, and nothing they do not. */
  uniforms(detail, minW, still) {
    const v = this.v;
    const haz = v.hazard;
    /* hard square-wave strobe: alert must be visible in peripheral vision */
    const strobe = still ? 0 : haz * (this.t * 3.1 % 1 < 0.5 ? 1 : 0);
    const intensity = clamp(this.intensity + this.impulse * 0.5, 0, 1);
    const gain = v.gain * (1 + 0.30 * strobe + 0.22 * this.impulse) * (0.92 + 0.16 * intensity);
    const ln = Math.hypot(this.look[0], this.look[1]) || 1;
    return {
      time: still ? 4.0 : this.t,
      colA: this.colA, colB: this.colB, colDeep: this.theme.deep,
      core: v.core, iris: v.iris, spread: v.spread, open: v.open,
      think: v.think, speak: v.speak, act: v.act, hazard: haz,
      intensity, breath: still ? 0 : v.breath, gain,
      look: [this.look[0] / ln * this.lookStrength, this.look[1] / ln * this.lookStrength],
      coreRot: this.coreRot, irisRot: this.irisRot, spin: v.spin,
      ringRot: this.ringRot, ringGain: this.ringGain,
      levelSeg: Math.round(clamp(intensity, 0, 1) * 24),
      motes: v.motes, flow: v.flow,
      detail, minW,
      chroma: 0.0055 * haz + 0.0008 * this.impulse,
      tear: haz * (still ? 0 : 1),
      state: this.state,
    };
  }
}

/* ==========================================================================
   RENDERER — WebGL2
   ========================================================================== */

const FILAMENT_SEG = 20;

class GLRenderer {
  constructor(canvas) {
    const attrs = {
      alpha: true, premultipliedAlpha: true, antialias: false, depth: false,
      stencil: false, desynchronized: true, powerPreference: 'low-power',
      preserveDrawingBuffer: true, failIfMajorPerformanceCaveat: false,
    };
    const gl = canvas.getContext('webgl2', attrs);
    if (!gl) throw new Error('webgl2 unavailable');
    this.gl = gl;
    this.canvas = canvas;
    this.backend = 'webgl2';

    this.hdr = !!(gl.getExtension('EXT_color_buffer_half_float') || gl.getExtension('EXT_color_buffer_float'));

    this.scene = makeProgram(gl, VS_FULL, FS_SCENE);
    this.fil = makeProgram(gl, VS_FILAMENT, FS_FILAMENT);
    this.mote = makeProgram(gl, VS_MOTE, FS_MOTE);
    this.bright = makeProgram(gl, VS_FULL, FS_BRIGHT);
    this.blur = makeProgram(gl, VS_FULL, FS_BLUR);
    this.comp = makeProgram(gl, VS_FULL, FS_COMPOSITE);

    this.tScene = new Target(gl, this.hdr);
    this.tA = new Target(gl, this.hdr);
    this.tB = new Target(gl, this.hdr);

    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    this.w = 0; this.h = 0;
    /* Per-pass gates. Public on purpose: it is how you bisect a rendering
       artefact in ten seconds from the browser console instead of guessing. */
    this.passes = { scene: true, filaments: true, motes: true, bloom: true };

    this._lost = false;
    this._onLost = (ev) => { ev.preventDefault(); this._lost = true; };
    canvas.addEventListener('webglcontextlost', this._onLost, false);
  }

  resize(w, h, bloomDiv) {
    this.w = w; this.h = h;
    this.tScene.size(w, h);
    const bw = Math.max(1, Math.floor(w / bloomDiv));
    const bh = Math.max(1, Math.floor(h / bloomDiv));
    this.tA.size(bw, bh);
    this.tB.size(bw, bh);
  }

  draw(u, q, theme) {
    if (this._lost) return;
    const gl = this.gl;
    const w = this.w, h = this.h;
    const m = Math.min(w, h);
    const scale = [m / w, m / h];
    gl.bindVertexArray(this.vao);

    /* ---- pass 1: the analytic being ---- */
    this.tScene.bind();
    gl.disable(gl.BLEND);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const s = this.scene;
    gl.useProgram(s.prog);
    gl.uniform2f(s.u.uRes, w, h);
    gl.uniform1f(s.u.uTime, u.time);
    gl.uniform3fv(s.u.uColA, u.colA);
    gl.uniform3fv(s.u.uColB, u.colB);
    gl.uniform3fv(s.u.uColDeep, u.colDeep);
    gl.uniform1f(s.u.uCore, u.core);
    gl.uniform1f(s.u.uIris, u.iris);
    gl.uniform1f(s.u.uSpread, u.spread);
    gl.uniform1f(s.u.uOpen, u.open);
    gl.uniform1f(s.u.uThink, u.think);
    gl.uniform1f(s.u.uSpeak, u.speak);
    gl.uniform1f(s.u.uAct, u.act);
    gl.uniform1f(s.u.uHazard, u.hazard);
    gl.uniform1f(s.u.uIntensity, u.intensity);
    gl.uniform1f(s.u.uBreath, u.breath);
    gl.uniform1f(s.u.uGain, u.gain);
    gl.uniform2fv(s.u.uLook, u.look);
    gl.uniform1f(s.u.uCoreRot, u.coreRot);
    gl.uniform1f(s.u.uIrisRot, u.irisRot);
    gl.uniform1f(s.u.uSpin, u.spin);
    gl.uniform1fv(s.u.uRingRot, u.ringRot);
    gl.uniform1fv(s.u.uRingGain, u.ringGain);
    gl.uniform1i(s.u.uLevelSeg, u.levelSeg);
    gl.uniform1f(s.u.uMinW, u.minW);
    gl.uniform1f(s.u.uDetail, u.detail);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    /* ---- pass 2: filaments, additive ---- */
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    const threads = clamp(u.think * 0.9 + u.act * 0.5 + u.open * 0.35 + u.intensity * 0.4, 0, 1);
    const nFil = this.passes.filaments ? Math.round(q.filaments * (0.30 + 0.70 * threads)) : 0;
    if (nFil > 0) {
      const f = this.fil;
      gl.useProgram(f.prog);
      gl.uniform1f(f.u.uTime, u.time);
      gl.uniform1f(f.u.uCore, u.core);
      gl.uniform1f(f.u.uSpread, u.spread);
      gl.uniform1f(f.u.uThreads, threads);
      gl.uniform1f(f.u.uWidth, Math.max(0.0035, u.minW * 1.3));
      gl.uniform1f(f.u.uActive, nFil);
      gl.uniform2fv(f.u.uScale, scale);
      gl.uniform3fv(f.u.uColA, u.colA);
      gl.uniform3fv(f.u.uColB, u.colB);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, (FILAMENT_SEG + 1) * 2, nFil);
    }

    /* ---- pass 3: motes ---- */
    const nMote = this.passes.motes ? Math.round(q.motes * u.motes) : 0;
    if (nMote > 0) {
      const mo = this.mote;
      gl.useProgram(mo.prog);
      gl.uniform1f(mo.u.uTime, u.time);
      gl.uniform1f(mo.u.uCore, u.core);
      gl.uniform1f(mo.u.uSpread, u.spread);
      gl.uniform1f(mo.u.uFlow, u.flow);
      gl.uniform1f(mo.u.uActive, nMote);
      gl.uniform1f(mo.u.uSpin, u.spin);
      gl.uniform1f(mo.u.uSizeScale, Math.max(1, u.minW / 0.0030));
      gl.uniform2fv(mo.u.uScale, scale);
      gl.uniform3fv(mo.u.uColA, u.colA);
      gl.uniform3fv(mo.u.uColB, u.colB);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, nMote);
    }
    gl.disable(gl.BLEND);

    /* ---- pass 4: bloom ---- */
    let bloomAmt = 0;
    if (q.bloom && this.passes.bloom && theme.bloom > 0.001) {
      bloomAmt = theme.bloom;
      const bw = this.tA.w, bh = this.tA.h;
      const b = this.bright;
      this.tA.bind();
      gl.useProgram(b.prog);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.tScene.tex);
      gl.uniform1i(b.u.uTex, 0);
      gl.uniform2f(b.u.uTexel, 1 / bw, 1 / bh);
      gl.uniform1f(b.u.uThresh, this.hdr ? 0.62 : 0.48);
      gl.drawArrays(gl.TRIANGLES, 0, 3);

      /* Two separable iterations, the second at 2.6x the offset. Four cheap
         5-tap passes buy a glow radius a single pass would need 30 taps for,
         and the wide soft falloff is the entire difference between "HUD" and
         "dark UI with a border". */
      const bl = this.blur;
      gl.useProgram(bl.prog);
      gl.uniform1i(bl.u.uTex, 0);
      gl.uniform2f(bl.u.uTexel, 1 / bw, 1 / bh);
      for (const step of [1.0, 2.6]) {
        this.tB.bind();
        gl.bindTexture(gl.TEXTURE_2D, this.tA.tex);
        gl.uniform2f(bl.u.uDir, step, 0);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        this.tA.bind();
        gl.bindTexture(gl.TEXTURE_2D, this.tB.tex);
        gl.uniform2f(bl.u.uDir, 0, step);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
    }

    /* ---- pass 5: composite to the screen ---- */
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const c = this.comp;
    gl.useProgram(c.prog);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.tScene.tex);
    gl.uniform1i(c.u.uScene, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, bloomAmt > 0 ? this.tA.tex : this.tScene.tex);
    gl.uniform1i(c.u.uBloom, 1);
    gl.uniform2f(c.u.uRes, w, h);
    gl.uniform1f(c.u.uBloomAmt, bloomAmt);
    gl.uniform1f(c.u.uGrain, theme.grain * q.grain);
    gl.uniform1f(c.u.uScan, theme.scan * u.detail);
    gl.uniform1f(c.u.uChroma, u.chroma);
    gl.uniform1f(c.u.uTear, u.tear);
    gl.uniform1f(c.u.uTime, u.time);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.activeTexture(gl.TEXTURE0);
  }

  destroy() {
    const gl = this.gl;
    this.canvas.removeEventListener('webglcontextlost', this._onLost, false);
    for (const p of [this.scene, this.fil, this.mote, this.bright, this.blur, this.comp]) {
      if (p) gl.deleteProgram(p.prog);
    }
    this.tScene.destroy(); this.tA.destroy(); this.tB.destroy();
    gl.deleteVertexArray(this.vao);
    const lose = gl.getExtension('WEBGL_lose_context');
    if (lose) lose.loseContext();
  }
}

/* ==========================================================================
   RENDERER — Canvas2D
   For WKWebViews with WebGL disabled, software-rendered VMs, and headless
   checks. Same animation model, same silhouette: rings, iris, lattice, level
   arc, filaments, motes. It is quieter, not different.
   ========================================================================== */

class C2DRenderer {
  constructor(canvas) {
    const ctx = canvas.getContext('2d', { alpha: true, desynchronized: true });
    if (!ctx) throw new Error('2d context unavailable');
    this.ctx = ctx;
    this.canvas = canvas;
    this.backend = 'canvas2d';
    this.w = 0; this.h = 0;
  }
  resize(w, h) { this.w = w; this.h = h; }

  draw(u, q) {
    const ctx = this.ctx;
    const w = this.w, h = this.h;
    const m = Math.min(w, h) / 2;
    const rgb = (c, a) => `rgba(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0},${a})`;
    const A = u.colA, B = u.colB;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.translate(w / 2, h / 2);
    ctx.scale(m, m);
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineCap = 'butt';

    const px = 1 / m;
    const sp = u.spread * (1 + 0.03 * u.breath * Math.sin(u.time * 1.45));
    const cr = u.core;

    /* deep field */
    const halo = ctx.createRadialGradient(0, 0, 0, 0, 0, 1.0);
    halo.addColorStop(0, rgb(A, 0.22 * u.gain));
    halo.addColorStop(0.35, rgb(A, 0.06 * u.gain));
    halo.addColorStop(1, rgb(A, 0));
    ctx.fillStyle = halo;
    ctx.fillRect(-1.4, -1.4, 2.8, 2.8);

    /* ring assembly */
    const RR = [0.285, 0.350, 0.420, 0.487, 0.575, 0.672, 0.775, 0.870];
    const RW = [0.0055, 0.0130, 0.0022, 0.0180, 0.0030, 0.0095, 0.0165, 0.0038];
    const RC = [64, 3, 1, 112, 1, 5, 8, 48];
    const RD = [0.34, 0.255, 1.0, 0.50, 1.0, 0.155, 0.105, 0.30];
    for (let i = 0; i < 8; i++) {
      const g = u.ringGain[i];
      if (g < 0.02) continue;
      const R = RR[i] * sp;
      const lw = Math.max(RW[i] * 2, px * 1.1);
      ctx.lineWidth = lw;
      ctx.strokeStyle = rgb(mixc(A, B, 0.3), 0.55 * g);
      const cnt = RC[i] > 8 ? Math.max(8, Math.floor(RC[i] * lerp(0.22, 1, u.detail))) : RC[i];
      if (RD[i] > 0.99) {
        ctx.beginPath(); ctx.arc(0, 0, R, 0, Math.PI * 2); ctx.stroke();
      } else {
        const step = (Math.PI * 2) / cnt;
        const half = step * RD[i] * 0.5;
        const rot = u.ringRot[i] * Math.PI * 2;
        for (let k = 0; k < cnt; k++) {
          const a = k * step - rot;
          ctx.beginPath(); ctx.arc(0, 0, R, a - half, a + half); ctx.stroke();
        }
      }
    }

    /* level arc */
    {
      const R = RR[4] * sp;
      const lvl = u.act > 0.5 ? u.levelSeg / 24 : u.intensity * 0.82;
      ctx.lineWidth = Math.max(0.015, px * 2);
      ctx.strokeStyle = rgb(mixc(B, [1, 1, 1], 0.25), 0.35 + 0.6 * u.intensity);
      ctx.beginPath();
      ctx.arc(0, 0, R, -Math.PI / 2, -Math.PI / 2 + lvl * Math.PI * 2);
      ctx.stroke();
    }

    /* hazard band */
    if (u.hazard > 0.02) {
      const R = 0.925 * sp;
      ctx.lineWidth = Math.max(0.048, px * 4);
      ctx.strokeStyle = rgb(A, 0.75 * u.hazard);
      const step = (Math.PI * 2) / 11;
      for (let k = 0; k < 11; k++) {
        const a = k * step;
        ctx.beginPath(); ctx.arc(0, 0, R, a, a + step * 0.5); ctx.stroke();
      }
    }

    /* filaments */
    const threads = clamp(u.think * 0.9 + u.act * 0.5 + u.open * 0.35, 0, 1);
    const nFil = Math.min(q.filaments, Math.round(q.filaments * (0.25 + 0.75 * threads)));
    ctx.lineWidth = Math.max(0.004, px * 1.2);
    for (let i = 0; i < nFil; i++) {
      const seed = i * 1.6180339887;
      const cyc = u.time * (0.30 + 1.75 * threads) * (0.55 + 0.9 * frac(seed * 91.7)) + frac(seed * 37.1) * 13;
      const life = cyc - Math.floor(cyc);
      const ep = Math.floor(cyc);
      const a0 = frac(seed * 57.13 + ep * 131.7) * Math.PI * 2;
      const a1 = a0 + (frac(seed * 13.7 + ep * 77.3) - 0.5) * 2.7;
      const r0 = cr * (0.8 + 0.22 * frac(seed * 21.1 + ep * 5.3));
      const r1 = cr + (0.09 + 0.6 * frac(seed * 9.3 + ep * 41.7)) * sp;
      const P0 = [Math.cos(a0) * r0, Math.sin(a0) * r0];
      const P2 = [Math.cos(a1) * r1, Math.sin(a1) * r1];
      const dx = P2[0] - P0[0], dy = P2[1] - P0[1];
      const len = Math.hypot(dx, dy) || 1;
      const bow = (frac(seed * 3.7 + ep * 19.1) - 0.5) * 0.75 * len;
      const P1 = [(P0[0] + P2[0]) / 2 - (dy / len) * bow, (P0[1] + P2[1]) / 2 + (dx / len) * bow];
      const env = smooth01(life / 0.05) * smooth01((1 - life) / 0.3);
      ctx.strokeStyle = rgb(mixc(A, B, 0.6), 0.65 * env);
      ctx.beginPath();
      ctx.moveTo(P0[0], P0[1]);
      ctx.quadraticCurveTo(P1[0], P1[1], P2[0], P2[1]);
      ctx.stroke();
    }

    /* motes */
    const nMote = Math.round(q.motes * u.motes * 0.5);
    ctx.fillStyle = rgb(mixc(A, B, 0.5), 0.5);
    for (let i = 0; i < nMote; i++) {
      const seed = i * 0.7548776662;
      const ang0 = frac(seed * 57.13) * Math.PI * 2;
      const rBase = cr * 1.2 + frac(seed * 21.7) * 0.66 * sp;
      const f = Math.min(1, Math.abs(u.flow));
      const ph = frac(u.time * (0.1 + 0.26 * frac(seed * 11.3)) * (0.4 + f) + frac(seed * 5.9));
      const travel = u.flow < 0 ? 1 - ph : ph;
      const r = lerp(rBase, lerp(cr * 1.02, rBase, travel), f);
      const ang = ang0 + u.time * (0.05 + 0.3 * frac(seed * 3.1)) * (0.35 + u.spin);
      const size = Math.max(0.004, px * 1.3);
      ctx.globalAlpha = 0.25 + 0.5 * Math.sin(ph * Math.PI) * f + 0.25 * (1 - f);
      ctx.fillRect(Math.cos(ang) * r - size, Math.sin(ang) * r - size, size * 2, size * 2);
    }
    ctx.globalAlpha = 1;

    /* speaking petals */
    if (u.speak > 0.02) {
      ctx.lineWidth = Math.max(0.005, px * 1.5);
      ctx.strokeStyle = rgb(mixc(A, B, 0.55), 0.5 + 0.5 * u.intensity);
      ctx.beginPath();
      for (let k = 0; k <= 96; k++) {
        const aq = (k / 96) * Math.PI * 2;
        let spec = 0;
        for (let j = 1; j <= 4; j++) spec += Math.sin(aq * (j * 3 + 1) + u.time * (2.2 + j * 1.9) + j * 1.7) / j;
        spec = spec * 0.42 + 0.5;
        const pl = cr + 0.018 + (0.02 + 0.2 * u.intensity) * spec;
        const x = Math.cos(aq) * pl, y = Math.sin(aq) * pl;
        if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath(); ctx.stroke();
    }

    /* core: lattice suggestion, shell, iris blades, nucleus */
    const coreGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, cr);
    coreGrad.addColorStop(0, rgb(B, 0.30));
    coreGrad.addColorStop(1, rgb(A, 0.06));
    ctx.fillStyle = coreGrad;
    ctx.beginPath(); ctx.arc(0, 0, cr, 0, Math.PI * 2); ctx.fill();

    ctx.lineWidth = Math.max(0.003, px * 1.1);
    ctx.strokeStyle = rgb(mixc(A, B, 0.5), 0.32 + 0.5 * u.think);
    for (let k = 0; k < 6; k++) {
      const a = (k / 6) * Math.PI * 2 + u.coreRot;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * cr * 0.16, Math.sin(a) * cr * 0.16);
      ctx.lineTo(Math.cos(a) * cr * 0.94, Math.sin(a) * cr * 0.94);
      ctx.stroke();
    }
    for (let k = 1; k <= 2; k++) {
      ctx.beginPath();
      ctx.arc(0, 0, cr * (0.34 + 0.3 * k), 0, Math.PI * 2);
      ctx.stroke();
    }

    ctx.lineWidth = Math.max(0.0045, px * 1.4);
    ctx.strokeStyle = rgb(mixc(B, [1, 1, 1], 0.3), 0.9);
    ctx.beginPath(); ctx.arc(0, 0, cr, 0, Math.PI * 2); ctx.stroke();

    /* iris: cover the lattice outside the aperture with the deep colour */
    const apr = cr * (0.1 + 0.74 * u.iris);
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = rgb(u.colDeep, 0.72);
    ctx.beginPath();
    ctx.arc(0, 0, cr * 0.995, 0, Math.PI * 2);
    ctx.arc(0, 0, apr, 0, Math.PI * 2, true);
    ctx.fill();
    ctx.globalCompositeOperation = 'lighter';

    ctx.lineWidth = Math.max(0.003, px);
    ctx.strokeStyle = rgb(B, 0.35);
    for (let k = 0; k < 8; k++) {
      const a = (k / 8) * Math.PI * 2 + u.irisRot;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * apr, Math.sin(a) * apr);
      ctx.lineTo(Math.cos(a) * cr, Math.sin(a) * cr);
      ctx.stroke();
    }
    ctx.lineWidth = Math.max(0.004, px * 1.3);
    ctx.strokeStyle = rgb(mixc(B, [1, 1, 1], 0.45), 0.95);
    ctx.beginPath(); ctx.arc(0, 0, apr, 0, Math.PI * 2); ctx.stroke();

    const nucleus = ctx.createRadialGradient(0, 0, 0, 0, 0, Math.max(apr, 0.02) * 1.6);
    nucleus.addColorStop(0, rgb([1, 1, 1], 0.85 * (0.6 + 0.5 * u.intensity)));
    nucleus.addColorStop(0.4, rgb(mixc(B, [1, 1, 1], 0.5), 0.5));
    nucleus.addColorStop(1, rgb(A, 0));
    ctx.fillStyle = nucleus;
    ctx.beginPath(); ctx.arc(0, 0, Math.max(apr, 0.02) * 1.6, 0, Math.PI * 2); ctx.fill();

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
  }
  destroy() {}
}

const frac = (x) => { const v = Math.sin(x) * 43758.5453; return v - Math.floor(v); };
const smooth01 = (x) => { const t = clamp(x, 0, 1); return t * t * (3 - 2 * t); };

/* ==========================================================================
   THE ENTITY
   ========================================================================== */

const ENTITY_CSS = `
.ars-entity-host { position: relative; display: block; overflow: hidden; }
.ars-entity-canvas {
  display: block; width: 100%; height: 100%;
  background: transparent; pointer-events: none;
}
`;

let cssInjected = false;
function injectCss() {
  if (cssInjected || typeof document === 'undefined') return;
  cssInjected = true;
  const style = document.createElement('style');
  style.id = 'ars-entity-style';
  style.textContent = ENTITY_CSS;
  document.head.appendChild(style);
}

export class ArsEntity {
  /**
   * @param {HTMLElement|HTMLCanvasElement} mount  a canvas, or any element to fill
   * @param {{size?: 'auto'|number, quality?: 'auto'|number}} [opts]
   */
  constructor(mount, opts = {}) {
    if (!mount) throw new Error('ArsEntity: no mount element');
    injectCss();

    this.mount = mount;
    this.opts = { size: 'auto', quality: 'auto', ...opts };
    this.destroyed = false;

    if (mount instanceof HTMLCanvasElement) {
      this.canvas = mount;
      this.host = mount.parentElement || mount;
    } else {
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'ars-entity-canvas';
      mount.classList.add('ars-entity-host');
      mount.appendChild(this.canvas);
      this.host = mount;
    }
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', 'A.R.S — idle');

    this.theme = readTheme(document.documentElement);
    this.model = new Model(this.theme);

    /* quality */
    this.qualityMode = this.opts.quality;
    this.level = this.qualityMode === 'auto' ? this._guessLevel() : clamp(this.qualityMode | 0, 0, QUALITY.length - 1);

    /* renderer, with a hard guarantee that construction never throws out */
    this.renderer = null;
    try {
      this.renderer = new GLRenderer(this.canvas);
    } catch (err) {
      this._glError = err && err.message ? err.message : String(err);
      try {
        this.renderer = new C2DRenderer(this.canvas);
      } catch (err2) {
        this._c2dError = err2 && err2.message ? err2.message : String(err2);
      }
    }

    /* motion preference */
    this.mqMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    this.still = !!(this.mqMotion && this.mqMotion.matches);
    this._onMotion = () => { this.still = this.mqMotion.matches; this._kick(); };
    if (this.mqMotion) {
      if (this.mqMotion.addEventListener) this.mqMotion.addEventListener('change', this._onMotion);
      else if (this.mqMotion.addListener) this.mqMotion.addListener(this._onMotion);
    }

    /* theme follows the OS */
    this.mqScheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
    this._onScheme = () => this.refreshTheme();
    if (this.mqScheme && this.mqScheme.addEventListener) this.mqScheme.addEventListener('change', this._onScheme);

    /* sizing */
    this.cssW = 0; this.cssH = 0; this.bufW = 0; this.bufH = 0;
    this._onWinResize = () => this._resize();
    window.addEventListener('resize', this._onWinResize, { passive: true });
    if (typeof ResizeObserver !== 'undefined') {
      this.ro = new ResizeObserver(() => this._resize());
      this.ro.observe(this.host === this.canvas ? this.canvas : this.host);
    }

    /* pause when the tab is hidden — the local model wants the GPU back */
    this._onVisibility = () => {
      if (document.hidden) this._stop();
      else { this.lastT = 0; this._start(); }
    };
    document.addEventListener('visibilitychange', this._onVisibility);

    /* pause when scrolled out of view */
    this.visible = true;
    if (typeof IntersectionObserver !== 'undefined') {
      this.io = new IntersectionObserver((entries) => {
        this.visible = entries.some((e) => e.isIntersecting);
        if (this.visible && !document.hidden) { this.lastT = 0; this._start(); } else this._stop();
      }, { threshold: 0 });
      this.io.observe(this.canvas);
    }

    /* attention: the being's index marker tracks the pointer, gently */
    this._onPointer = (ev) => this.lookAt(ev.clientX, ev.clientY);
    this._onPointerOut = () => { this.model.lookStrengthTarget = 0.16; };
    window.addEventListener('pointermove', this._onPointer, { passive: true });
    window.addEventListener('pointerleave', this._onPointerOut, { passive: true });

    /* frame timing */
    this.lastT = 0;
    this.frameMs = 16.7;
    this.frames = 0;
    this.slow = 0;
    this.fast = 0;
    this.raf = 0;

    this._resize();
    this.model.step(0.016, { still: this.still });
    this._drawOnce();
    this._start();
  }

  /* ---------------------------------------------------------------- public */

  setState(state) {
    if (this.destroyed) return;
    this.model.setState(state);
    this.canvas.setAttribute('aria-label', `A.R.S — ${this.model.state}`);
    this._kick();
  }

  setIntensity(v) {
    if (this.destroyed) return;
    const n = Number(v);
    this.model.intensityTarget = Number.isFinite(n) ? clamp(n, 0, 1) : 0;
    this._kick();
  }

  setTier(tier) {
    if (this.destroyed) return;
    const key = tier == null ? null : String(tier).trim().toLowerCase();
    this.model.tier = key && ARS_TIERS.includes(key) ? key : null;
    this._kick();
  }

  /** A single visible beat — use it for "message received", "tool finished". */
  pulse(strength = 1) {
    if (this.destroyed) return;
    this.model.impulse = clamp(this.model.impulse + strength, 0, 1.5);
    this._kick();
  }

  /** Point the index marker at a viewport coordinate (e.g. a mouse position). */
  lookAt(x, y) {
    if (this.destroyed) return;
    const r = this.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const dx = x - (r.left + r.width / 2);
    const dy = y - (r.top + r.height / 2);
    const len = Math.hypot(dx, dy);
    if (len < 1e-3) return;
    /* screen y is down, the shader's y is up */
    this.model.lookTarget[0] = dx / len;
    this.model.lookTarget[1] = -dy / len;
    this.model.lookStrengthTarget = clamp(0.25 + 0.75 * Math.exp(-len / (r.width * 4)), 0.16, 1);
  }

  setQuality(q) {
    this.qualityMode = q;
    if (q !== 'auto') this.level = clamp(q | 0, 0, QUALITY.length - 1);
    this.slow = 0; this.fast = 0;
    this._resize();
    this._kick();
  }

  refreshTheme() {
    this.theme = readTheme(document.documentElement);
    this.model.theme = this.theme;
    this._kick();
  }

  stats() {
    return {
      backend: this.renderer ? this.renderer.backend : 'none',
      quality: this.level,
      qualityMode: this.qualityMode,
      fps: this.frameMs > 0 ? Math.round(1000 / this.frameMs) : 0,
      frameMs: Math.round(this.frameMs * 100) / 100,
      dpr: this.dpr,
      buffer: [this.bufW, this.bufH],
      css: [this.cssW, this.cssH],
      detail: Math.round(this.detail * 100) / 100,
      state: this.model.state,
      tier: this.model.tier,
      intensity: Math.round(this.model.intensity * 100) / 100,
      reducedMotion: this.still,
      running: !!this.raf,
      glError: this._glError || null,
    };
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this._stop();
    window.removeEventListener('resize', this._onWinResize);
    window.removeEventListener('pointermove', this._onPointer);
    window.removeEventListener('pointerleave', this._onPointerOut);
    document.removeEventListener('visibilitychange', this._onVisibility);
    if (this.ro) this.ro.disconnect();
    if (this.io) this.io.disconnect();
    if (this.mqMotion) {
      if (this.mqMotion.removeEventListener) this.mqMotion.removeEventListener('change', this._onMotion);
      else if (this.mqMotion.removeListener) this.mqMotion.removeListener(this._onMotion);
    }
    if (this.mqScheme && this.mqScheme.removeEventListener) this.mqScheme.removeEventListener('change', this._onScheme);
    if (this.renderer) { try { this.renderer.destroy(); } catch { /* context already gone */ } }
    if (this.canvas.parentElement && this.canvas.parentElement !== this.mount) this.canvas.remove();
    else if (this.mount !== this.canvas) this.canvas.remove();
    if (this.mount && this.mount.classList) this.mount.classList.remove('ars-entity-host');
  }

  /* --------------------------------------------------------------- internal */

  _guessLevel() {
    const dpr = window.devicePixelRatio || 1;
    const cores = navigator.hardwareConcurrency || 4;
    if (cores <= 4 && dpr <= 1) return 1;
    if (dpr >= 2 && cores >= 8) return 3;
    return 2;
  }

  _resize() {
    if (this.destroyed || !this.renderer) return;
    const q = QUALITY[this.level];
    let cssW;
    let cssH;
    if (typeof this.opts.size === 'number') {
      cssW = cssH = this.opts.size;
      this.canvas.style.width = `${cssW}px`;
      this.canvas.style.height = `${cssH}px`;
    } else {
      const box = (this.host === this.canvas ? this.canvas : this.host).getBoundingClientRect();
      cssW = Math.max(1, Math.round(box.width));
      cssH = Math.max(1, Math.round(box.height));
    }
    const dpr = Math.min(window.devicePixelRatio || 1, q.dpr);
    const bw = Math.max(1, Math.round(cssW * dpr));
    const bh = Math.max(1, Math.round(cssH * dpr));
    this.cssW = cssW; this.cssH = cssH; this.dpr = dpr;
    if (bw !== this.bufW || bh !== this.bufH) {
      this.bufW = bw; this.bufH = bh;
      this.canvas.width = bw;
      this.canvas.height = bh;
      this.renderer.resize(bw, bh, q.bloomDiv);
    }

    /* Size-aware presentation. pxPerUnit is how many device pixels one unit of
       the shader's space covers; every "is this feature even visible" decision
       hangs off it, which is what lets the same source be a 44px favicon and a
       4K centre stage without a second asset. */
    const pxPerUnit = Math.min(bw, bh) / 2;
    this.pxPerUnit = pxPerUnit;
    this.detail = clamp((pxPerUnit - 42) / 150, 0, 1);
    this.minW = 0.62 / Math.max(pxPerUnit, 1);

    /* Which rings survive at this size. Below ~90px across, eight concentric
       hairlines are grey mush; three well-spaced ones still read as a machine. */
    const keep = pxPerUnit < 62 ? [1, 4, 6]
      : pxPerUnit < 100 ? [0, 1, 4, 6]
      : pxPerUnit < 170 ? [0, 1, 3, 4, 6, 7]
      : [0, 1, 2, 3, 4, 5, 6, 7];
    for (let i = 0; i < 8; i++) this.model.ringGain[i] = keep.includes(i) ? 1 : 0;
    /* And the core grows as the assembly loses rings, so it stays the subject. */
    /* The assembly reaches 0.955 units at spread 1.0 and 1.10 at listening's
       1.15 — which clips. Everything is scaled to fit the widest pose, so the
       being never touches the edge of its own frame. */
    this.coreBoost = lerp(1.75, 1.0, this.detail);
    this.spreadBoost = lerp(0.84, 0.90, this.detail);
  }

  _start() {
    if (this.destroyed || this.raf || !this.renderer) return;
    if (document.hidden || !this.visible) return;
    if (this.still) { this._drawOnce(); return; }
    this.lastT = 0;
    this.raf = requestAnimationFrame(this._frame);
  }

  _stop() {
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
  }

  /** Nudge: after a state/tier/intensity change, make sure something redraws
   *  even if we are parked in reduced-motion or paused-but-visible. */
  _kick() {
    if (this.destroyed) return;
    if (this.still) {
      if (this._stillRaf) return;
      this._stillRaf = requestAnimationFrame(() => { this._stillRaf = 0; this._drawOnce(); });
    } else if (!this.raf) {
      this._start();
    }
  }

  _frame = (now) => {
    this.raf = 0;
    if (this.destroyed) return;
    const t0 = now || performance.now();
    let dt = this.lastT ? (t0 - this.lastT) / 1000 : 1 / 60;
    this.lastT = t0;
    dt = clamp(dt, 1 / 240, 1 / 12);   // a long stall must not teleport the being

    this.model.step(dt, { still: false });
    this._render();

    /* Adaptive quality: watch our own cost and give the GPU back to the model
       before the model starts stuttering because of us. */
    const spent = performance.now() - t0;
    this.frameMs = this.frameMs * 0.9 + (dt * 1000) * 0.1;
    this.cpuMs = (this.cpuMs || spent) * 0.9 + spent * 0.1;
    this.frames++;
    if (this.qualityMode === 'auto') {
      if (this.frameMs > 21) { this.slow++; this.fast = 0; } else if (this.frameMs < 14) { this.fast++; this.slow = 0; }
      if (this.slow > 45 && this.level > 0) { this.level--; this.slow = 0; this._resize(); }
      else if (this.fast > 400 && this.level < QUALITY.length - 1) { this.fast = 0; this.level++; this._resize(); }
    }

    this.raf = requestAnimationFrame(this._frame);
  };

  _drawOnce() {
    if (this.destroyed || !this.renderer) return;
    this.model.step(1 / 60, { still: this.still });
    this._render();
  }

  _render() {
    if (!this.renderer || !this.bufW) return;
    const u = this.model.uniforms(this.detail, this.minW, this.still);
    u.core *= this.coreBoost;
    u.spread *= this.spreadBoost;
    try {
      this.renderer.draw(u, QUALITY[this.level], this.theme);
    } catch (err) {
      /* A renderer that throws mid-frame must not take the page with it. */
      console.warn('[ars-entity] render failed, dropping to canvas2d', err);
      this._stop();
      try { this.renderer.destroy(); } catch { /* ignore */ }
      this.renderer = null;
      try {
        const c = document.createElement('canvas');
        c.className = this.canvas.className;
        this.canvas.replaceWith(c);
        this.canvas = c;
        this.renderer = new C2DRenderer(c);
        this.bufW = 0;
        this._resize();
        this._start();
      } catch { /* nothing left to fall back to; stay silent and blank */ }
    }
  }
}

export default ArsEntity;
