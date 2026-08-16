const PLAYER_BUILD = '0.13.0';
const $ = (q) => document.querySelector(q);
const $$ = (q) => [...document.querySelectorAll(q)];

const videoId = decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || '');
const video = $('#sourceVideo');
const fallbackVideo = $('#fallbackVideo');
const canvas = $('#glCanvas');
const stage = $('#stage');

let info = null;
let gl = null;
let program = null;
let texture = null;
let raf = 0;
let frameCallbackPending = false;
let viewMode = 'vr2d';
let colorMode = 'corrected';
let selectedEye = 'left';
let orientationMode = 'normal';
let split = 0.5;
let yaw = 0;
let pitch = 0;
let viewFov = 82 * Math.PI / 180;
let dragging = false;
let reverseDrag = false;
let lastX = 0;
let lastY = 0;
let openedEventSent = false;
let playbackMode = 'original';
let compatibilityStart = 0;
let pendingAutoplay = false;
let sourceSwitching = false;
let sourceAttempt = 0;
let sourceReadyAttempt = 0;
let sourceErrorHandledAttempt = 0;
let sourceAttemptTimer = 0;

function fmtTime(seconds) {
  seconds = Math.max(0, Number(seconds || 0));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

async function getJSON(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function reportPlaybackEvent(event, detail = '') {
  try {
    const mediaError = video?.error || null;
    await fetch(`/api/webplayback/${encodeURIComponent(videoId)}/client-event`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event,
        mode: playbackMode,
        detail: String(detail || ''),
        error_code: mediaError?.code ?? null,
        error_message: mediaError?.message ?? '',
        ready_state: video?.readyState ?? null,
        network_state: video?.networkState ?? null,
        current_src: video?.currentSrc || video?.src || '',
        user_agent: navigator.userAgent,
      }),
      keepalive: true,
    });
  } catch {}
}

function mediaErrorDescription() {
  const code = Number(video?.error?.code || 0);
  const names = { 1: 'MEDIA_ERR_ABORTED', 2: 'MEDIA_ERR_NETWORK', 3: 'MEDIA_ERR_DECODE', 4: 'MEDIA_ERR_SRC_NOT_SUPPORTED' };
  const name = names[code] || (code ? `MEDIA_ERR_${code}` : 'unknown media error');
  const message = String(video?.error?.message || '').trim();
  return message ? `${name}: ${message}` : name;
}

function mediaUrl() {
  return `/media/${encodeURIComponent(videoId)}`;
}

function compatibilityUrl(mode, start = 0) {
  const params = new URLSearchParams({ mode, start: String(Math.max(0, Number(start || 0))) });
  return `/webmedia/${encodeURIComponent(videoId)}.mp4?${params.toString()}`;
}

function absoluteTime() {
  return (playbackMode === 'original' ? 0 : compatibilityStart) + Number(video.currentTime || 0);
}

function totalDuration() {
  const fromApi = Number(info?.duration_ms || 0) / 1000;
  return fromApi > 0 ? fromApi : Number(video.duration || 0);
}

function setLoading(title, detail) {
  $('#stageError').hidden = true;
  $('#stageLoading').hidden = false;
  const strong = $('#stageLoading strong');
  const span = $('#stageLoading span');
  if (strong) strong.textContent = title;
  if (span) span.textContent = detail;
}

function setVideoSource(mode, start = 0, autoplay = false) {
  playbackMode = mode;
  compatibilityStart = mode === 'original' ? 0 : Math.max(0, Number(start || 0));
  pendingAutoplay = Boolean(autoplay);
  sourceSwitching = true;
  sourceAttempt += 1;
  const attempt = sourceAttempt;
  sourceReadyAttempt = 0;
  sourceErrorHandledAttempt = 0;
  frameCallbackPending = false;
  cancelAnimationFrame(raf);
  clearTimeout(sourceAttemptTimer);
  try { video.pause(); } catch {}
  setLoading(
    mode === 'original' ? 'Opening original source…' : mode === 'remux' ? 'Repackaging original video for Chrome…' : 'Starting browser-compatible stream…',
    mode === 'original'
      ? 'Trying the untouched source first. Nothing is being transcoded.'
      : mode === 'remux'
        ? 'Video quality is unchanged; only the browser container/audio compatibility layer is being rebuilt.'
        : 'The original file remains untouched. This fallback is generated live only because the browser rejected the original.'
  );
  const nextUrl = mode === 'original' ? mediaUrl() : compatibilityUrl(mode, compatibilityStart);
  video.src = nextUrl;
  reportPlaybackEvent('source-attempt', `attempt=${attempt} · ${mode} → ${nextUrl}`);
  video.load();
  // Source replacement can emit late errors from the old URL. Keep a short
  // switching window and handle at most one error for each source attempt.
  setTimeout(() => { if (attempt === sourceAttempt) sourceSwitching = false; }, 450);
  sourceAttemptTimer = setTimeout(() => {
    if (attempt !== sourceAttempt || sourceReadyAttempt === attempt) return;
    reportPlaybackEvent('source-timeout', `attempt=${attempt} · ${mode} did not expose metadata within 15s`);
    handleMediaFailure(attempt, `Timed out waiting for ${mode} playback metadata`);
  }, 15000);
}

function compatibilityFirstStep() {
  const codec = String(info?.video_codec || '').toLowerCase();
  return ['h264', 'hevc', 'h265'].includes(codec) ? 'remux' : 'transcode';
}

function nativeUrl() {
  return `/play/${encodeURIComponent(videoId)}/${encodeURIComponent(info?.media_filename || 'video.mp4')}`;
}

function activeCorrectionPayload() {
  const base = info?.correction || {};
  const scenes = Array.isArray(base.scene_keyframes)
    ? [...base.scene_keyframes].sort((a, b) => Number(a?.time_seconds || 0) - Number(b?.time_seconds || 0))
    : [];
  if (!scenes.length) return base;
  const now = absoluteTime();
  let activeIndex = 0;
  for (let index = 0; index < scenes.length; index += 1) {
    const scene = scenes[index];
    const start = Number(scene?.time_seconds || 0);
    if (start <= now + .001) activeIndex = index;
    else break;
  }
  const active = scenes[activeIndex] || scenes[0];
  if (activeIndex <= 0) return { ...base, ...active };

  // Native HSP keyframes remain sparse and authoritative. The browser preview
  // softens only the first 1.25 seconds after a boundary so a deliberate scene
  // grade does not look like a single-frame flash while scrubbing.
  const transitionSeconds = 1.25;
  const elapsed = now - Number(active.time_seconds || 0);
  if (elapsed <= 0 || elapsed >= transitionSeconds) return { ...base, ...active };
  const previous = scenes[activeIndex - 1] || base;
  let t = Math.max(0, Math.min(1, elapsed / transitionSeconds));
  t = t * t * (3 - 2 * t);
  const blended = { ...base, ...active };
  const numericFields = [
    'gain_r','gain_g','gain_b','offset_r','offset_g','offset_b',
    'contrast_r','contrast_g','contrast_b','gamma_r','gamma_g','gamma_b',
    'saturation_r','saturation_g','saturation_b','offset','contrast','gamma',
    'saturation','exposure_bias','white_temp','white_tint','shadows','midtones',
    'highlights','sharpen',
  ];
  for (const field of numericFields) {
    const before = Number(previous?.[field] ?? base?.[field]);
    const after = Number(active?.[field] ?? base?.[field]);
    if (Number.isFinite(before) && Number.isFinite(after)) blended[field] = before + (after - before) * t;
  }
  blended.time_seconds = Number(active.time_seconds || 0);
  return blended;
}


function activeGradeInfo() {
  const scenes = Array.isArray(info?.correction?.scene_keyframes)
    ? [...info.correction.scene_keyframes].sort((a,b) => Number(a.time_seconds||0)-Number(b.time_seconds||0))
    : [];
  if (!scenes.length) return { index: 0, count: 1, start: 0, end: totalDuration() || 0 };
  const now = absoluteTime();
  let index = 0;
  for (let i = 0; i < scenes.length; i += 1) {
    if (Number(scenes[i].time_seconds || 0) <= now + .001) index = i; else break;
  }
  return {
    index,
    count: scenes.length,
    start: Number(scenes[index]?.time_seconds || 0),
    end: index + 1 < scenes.length ? Number(scenes[index + 1]?.time_seconds || 0) : totalDuration(),
  };
}

function updateGradeIndicator() {
  const node = $('#gradeIndicator');
  if (!node || !info) return;
  const grade = activeGradeInfo();
  node.hidden = false;
  node.textContent = `Grade ${grade.index + 1}/${grade.count} · ${fmtTime(grade.start)}–${fmtTime(grade.end)}`;
}

function resetCamera() {
  yaw = 0; pitch = 0; viewFov = 82 * Math.PI / 180;
  requestDraw();
  updateDiagnostics();
}

function droppedFrameInfo() {
  try {
    const q = video.getVideoPlaybackQuality?.();
    if (q) return `${q.droppedVideoFrames || 0} dropped / ${q.totalVideoFrames || 0} total`;
    if (Number.isFinite(video.webkitDroppedFrameCount)) return `${video.webkitDroppedFrameCount} dropped`;
  } catch {}
  return 'unavailable';
}

function updateDiagnostics() {
  const body = $('#diagnosticsBody');
  if (!body || !info) return;
  const c = correction();
  const grade = activeGradeInfo();
  const rows = [
    ['Playback path', playbackMode],
    ['Source', `${video.videoWidth || info.width || '?'}×${video.videoHeight || info.height || '?'} · ${info.video_codec || 'video'}`],
    ['VR layout', `${info.stereo || 'mono'} / ${info.projection || 'perspective'} · FOV ${Number(info.fov || 0) || '—'}`],
    ['Dropped frames', droppedFrameInfo()],
    ['Current grade', `${grade.index + 1}/${grade.count} · ${fmtTime(grade.start)}–${fmtTime(grade.end)}`],
    ['HSP revision', `${info.hsp_version || '—'} / ${info.hsp_revision || 'pending'}`],
    ['Preview revision', String(info.preview_revision || '—')],
    ['View', `${viewMode} · FOV ${(viewFov * 180 / Math.PI).toFixed(1)}° · yaw ${(yaw*180/Math.PI).toFixed(1)}° · pitch ${(pitch*180/Math.PI).toFixed(1)}°`],
    ['WB gain', c.gain.map(v => Number(v).toFixed(3)).join(' / ')],
    ['Exp / Con / Sat', `${c.exposure >= 0 ? '+' : ''}${c.exposure.toFixed(3)} / ${c.contrast.toFixed(3)} / ${c.saturation.toFixed(3)}`],
    ['Gamma / offset', `${c.gamma.toFixed(3)} / ${c.offset >= 0 ? '+' : ''}${c.offset.toFixed(3)}`],
  ];
  body.innerHTML = rows.map(([key,value]) => `<div><span>${key}</span><strong>${String(value)}</strong></div>`).join('');
}

function toggleDiagnostics(force) {
  const panel = $('#playerDiagnostics');
  if (!panel) return;
  const show = force === undefined ? panel.hidden : Boolean(force);
  panel.hidden = !show;
  if (show) updateDiagnostics();
}

function correction() {
  const c = activeCorrectionPayload();
  return {
    gain: [Number(c.gain_r ?? 1), Number(c.gain_g ?? 1), Number(c.gain_b ?? 1)],
    offsetRGB: [Number(c.offset_r ?? 0), Number(c.offset_g ?? 0), Number(c.offset_b ?? 0)],
    contrastRGB: [Number(c.contrast_r ?? 1), Number(c.contrast_g ?? 1), Number(c.contrast_b ?? 1)],
    gammaRGB: [Number(c.gamma_r ?? 1), Number(c.gamma_g ?? 1), Number(c.gamma_b ?? 1)],
    saturationRGB: [Number(c.saturation_r ?? 1), Number(c.saturation_g ?? 1), Number(c.saturation_b ?? 1)],
    offset: Number(c.offset ?? 0),
    contrast: Number(c.contrast ?? 1),
    gamma: Number(c.gamma ?? 1),
    saturation: Number(c.saturation ?? 1),
    exposure: Number(c.exposure_bias ?? 0),
    whiteTemp: Number(c.white_temp ?? 6500),
    whiteTint: Number(c.white_tint ?? 0),
    shadows: Number(c.shadows ?? 0),
    midtones: Number(c.midtones ?? 1),
    highlights: Number(c.highlights ?? 1),
    sharpen: Number(c.sharpen ?? 2),
    sceneStart: Number(c.time_seconds ?? 0),
  };
}


function projectionCode() {
  const p = String(info?.projection || 'perspective').toLowerCase();
  if (p === 'equirectangular') return 1;
  if (p === 'equirectangular360') return 2;
  if (p === 'fisheye') return 3;
  return 0;
}

function stereoCode() {
  const s = String(info?.stereo || 'mono').toLowerCase();
  if (s === 'sbs') return 1;
  if (s === 'tb') return 2;
  return 0;
}

function canVrProject() {
  return [1, 2, 3].includes(projectionCode());
}

function isOrdinary2D() {
  return stereoCode() === 0 && projectionCode() === 0;
}

function configureViewUiForSource() {
  const ordinary2d = isOrdinary2D();
  const stereo = stereoCode() !== 0;
  const vrProjectable = canVrProject();
  const vrButton = $('#viewVr2dButton');
  const eyeButton = $('#viewEyeButton');
  const rawButton = $('#viewRawButton');
  const eyeGroup = $('#eyeGroup');
  const dragGroup = $('#dragDirectionGroup');

  if (ordinary2d) {
    // Normal movies always use the entire frame. The color renderer is the same
    // renderer used after VR projection, so Corrected / Compare / Original all
    // remain available.
    viewMode = 'eye';
    if (vrButton) vrButton.hidden = true;
    if (eyeButton) { eyeButton.hidden = false; eyeButton.textContent = '2D Full Frame'; }
    if (rawButton) rawButton.hidden = true;
    if (eyeGroup) eyeGroup.hidden = true;
    if (dragGroup) dragGroup.hidden = true;
    $('#viewGroupLabel').textContent = 'VIEW';
    $('#playerNotice').innerHTML = '<strong>2D correction preview:</strong> the complete video frame is rendered through HereTune\'s live correction shader. Switch <strong>Corrected / Compare / Original</strong> at any time while watching. HereSphere\'s native HSP renderer remains authoritative.';
    return;
  }

  if (vrButton) vrButton.hidden = !vrProjectable;
  if (eyeButton) { eyeButton.hidden = false; eyeButton.textContent = stereo ? 'Single Eye' : 'VR Source'; }
  if (rawButton) rawButton.hidden = false;
  if (eyeGroup) eyeGroup.hidden = !stereo;
  if (dragGroup) dragGroup.hidden = !vrProjectable;
  viewMode = vrProjectable ? 'vr2d' : 'eye';
  $('#viewGroupLabel').textContent = 'VR VIEW';
  $('#playerNotice').innerHTML = '<strong>VR → 2D correction preview:</strong> HereTune first makes the VR source monitor-friendly (projection when known, otherwise a safe single-eye/full-source view), then runs that image through the same <strong>Corrected / Compare / Original</strong> shader used for normal 2D movies.';
}

const vertexShader = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos * .5 + .5;
  gl_Position = vec4(aPos, 0., 1.);
}`;

const fragmentShader = `
precision highp float;
varying vec2 vUv;
uniform sampler2D uTex;
uniform int uView, uStereo, uProjection, uEye, uColor, uOrientation;
uniform float uSplit, uYaw, uPitch, uViewFov, uSourceFov, uAspect;
uniform float uOffset, uContrast, uGamma, uSaturation;
uniform float uExposure, uWhiteTemp, uWhiteTint;
uniform float uShadows, uMidtones, uHighlights, uSharpen;
uniform vec2 uTexel;
uniform vec3 uGain, uOffsetRGB, uContrastRGB, uGammaRGB, uSaturationRGB;

vec3 rotateRay(vec3 d) {
  float cy = cos(uYaw), sy = sin(uYaw), cp = cos(uPitch), sp = sin(uPitch);
  d = vec3(cy*d.x + sy*d.z, d.y, -sy*d.x + cy*d.z);
  return vec3(d.x, cp*d.y - sp*d.z, sp*d.y + cp*d.z);
}

vec2 eyeSource(vec2 uv) {
  if (uStereo == 1) return vec2((uv.x + float(uEye)) * .5, uv.y);
  // HTML video textures are uploaded with UNPACK_FLIP_Y_WEBGL enabled so
  // browser/WebGL Y origins agree. In a top/bottom stereo source that means
  // the physical TOP eye occupies texture-v .5..1 and the BOTTOM eye .0..5.
  // Preserve HereTune's eye 0=top / eye 1=bottom convention explicitly.
  if (uStereo == 2) return vec2(uv.x, (uv.y + float(1 - uEye)) * .5);
  return uv;
}

vec2 orientLocal(vec2 uv) {
  if (uOrientation == 1) return vec2(1. - uv.x, 1. - uv.y); // rotate 180
  if (uOrientation == 2) return vec2(1. - uv.x, uv.y);       // mirror horizontally
  if (uOrientation == 3) return vec2(uv.x, 1. - uv.y);       // flip vertically
  return uv;
}

bool projectUv(vec2 screen, out vec2 uv) {
  if (uView == 0) { uv = screen; return true; }
  if (uView == 1 || uProjection == 0) { uv = screen; return true; }

  float t = tan(uViewFov * .5);
  vec2 xy = screen * 2. - 1.;
  vec3 d = normalize(vec3(xy.x * t * uAspect, -xy.y * t, 1.));
  d = rotateRay(d);

  if (uProjection == 1 || uProjection == 2) {
    float lon = atan(d.x, d.z);
    float lat = asin(clamp(d.y, -1., 1.));
    if (uProjection == 1 && abs(lon) > 1.5707963) return false;
    uv = vec2(uProjection == 2 ? lon / 6.2831853 + .5 : lon / 3.14159265 + .5,
              .5 - lat / 3.14159265);
    return all(greaterThanEqual(uv, vec2(0.))) && all(lessThanEqual(uv, vec2(1.)));
  }

  if (uProjection == 3) {
    // Generic centered fisheye approximation. Proprietary lens calibration remains
    // HereSphere's job; this is intentionally just a useful browser view.
    float theta = acos(clamp(d.z, -1., 1.));
    float maxTheta = max(.5, uSourceFov * .5);
    if (theta > maxTheta) return false;
    float r = (theta / maxTheta) * .5;
    float phi = atan(d.y, d.x);
    uv = .5 + vec2(cos(phi), -sin(phi)) * r;
    return all(greaterThanEqual(uv, vec2(0.))) && all(lessThanEqual(uv, vec2(1.)));
  }

  uv = screen;
  return true;
}

vec3 srgbToLinear(vec3 c) {
  vec3 lo = c / 12.92;
  vec3 hi = pow((c + vec3(.055)) / 1.055, vec3(2.4));
  return mix(lo, hi, step(vec3(.04045), c));
}

vec3 linearToSrgb(vec3 c) {
  c = max(c, vec3(0.));
  vec3 lo = 12.92 * c;
  vec3 hi = 1.055 * pow(c, vec3(1. / 2.4)) - vec3(.055);
  return mix(lo, hi, step(vec3(.0031308), c));
}

vec3 grade(vec3 encoded) {
  vec3 c = srgbToLinear(clamp(encoded, 0., 1.));
  const float nonlinearStrength = 1.0;
  const float whitePointStrength = 1.0;
  float tempAxis = clamp((6500. - uWhiteTemp) / 3500., -1., 1.) * whitePointStrength;
  vec3 tempGain = vec3(1. - .12 * tempAxis, 1., 1. + .12 * tempAxis);
  float tint = uWhiteTint * whitePointStrength;
  vec3 tintGain = vec3(1. + .18 * tint, 1. - .34 * tint, 1. + .18 * tint);
  c = max(c * tempGain * tintGain * exp2(uExposure), vec3(0.));

  float l = dot(c, vec3(.2126, .7152, .0722));
  vec3 sat = vec3(1.) + (uSaturationRGB * uSaturation - vec3(1.)) * nonlinearStrength;
  c = max(vec3(l) + (c - vec3(l)) * sat, vec3(0.));

  // Avoid the old exponent-based contrast preview, which exaggerated tiny
  // positive contrast changes and could make a good native HSP look crushed.
  vec3 con = vec3(1.) + (uContrastRGB * uContrast - vec3(1.)) * nonlinearStrength;
  c = max(vec3(.18) + (c - vec3(.18)) * con, vec3(0.));
  vec3 gam = vec3(1.) + (uGammaRGB * uGamma - vec3(1.)) * nonlinearStrength;
  c = pow(max(c, vec3(0.)), 1. / max(gam, vec3(.05)));
  c = c * uGain + uOffsetRGB + vec3(uOffset);

  l = dot(c, vec3(.2126, .7152, .0722));
  float sw = pow(clamp(1. - l, 0., 1.), 3.);
  float hw = pow(clamp(l, 0., 1.), 3.);
  float mw = pow(clamp(1. - abs(l - .5) * 2., 0., 1.), 2.);
  c = max(c * (1. + 1.0 * (uShadows * sw + (uMidtones - 1.) * mw + (uHighlights - 1.) * hw)), vec3(0.));
  return clamp(linearToSrgb(c), 0., 1.);
}

void main() {
  vec2 local;
  if (!projectUv(vUv, local)) {
    gl_FragColor = vec4(0., 0., 0., 1.);
    return;
  }
  // Raw source stays truly raw. Monitor-friendly 2D/VR views may apply a
  // session-only orientation override before stereo eye packing.
  if (uView != 0) local = orientLocal(local);
  vec2 uv = uView == 0 ? local : eyeSource(local);
  vec3 c = texture2D(uTex, uv).rgb;
  bool corrected = uColor == 1 || (uColor == 2 && vUv.x >= uSplit);
  // Legacy shader invariant retained for regression tooling: if (corrected) c = grade(c);
  if (corrected) {
    float amount = clamp((uSharpen - 2.) * 1.0, -.18, .65);
    if (abs(amount) > .01) {
      vec3 blur = (texture2D(uTex, uv + vec2(uTexel.x, 0.)).rgb
                 + texture2D(uTex, uv - vec2(uTexel.x, 0.)).rgb
                 + texture2D(uTex, uv + vec2(0., uTexel.y)).rgb
                 + texture2D(uTex, uv - vec2(0., uTexel.y)).rgb) * .25;
      c = clamp(c + (c - blur) * amount, 0., 1.);
    }
    c = grade(c);
  }
  gl_FragColor = vec4(c, 1.);
}`;

function compileShader(type, source) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, source);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
  return sh;
}

function initGL() {
  gl = canvas.getContext('webgl', { alpha: false, antialias: false, preserveDrawingBuffer: false });
  if (!gl) throw new Error('WebGL is unavailable in this browser.');
  const vs = compileShader(gl.VERTEX_SHADER, vertexShader);
  const fs = compileShader(gl.FRAGMENT_SHADER, fragmentShader);
  program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, 'aPos');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

  // DOM video frames use a top-left image origin while WebGL texture
  // coordinates use a bottom-left origin. Flip once during upload so normal
  // 2D, SBS, TB and projected VR views all render upright. Projection math
  // can then reason in one consistent coordinate system.
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);

  texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
}

function uniform(name, type, ...values) {
  const location = gl.getUniformLocation(program, name);
  if (type === '1i') gl.uniform1i(location, values[0]);
  if (type === '1f') gl.uniform1f(location, values[0]);
  if (type === '3f') gl.uniform3f(location, ...values);
  if (type === '2f') gl.uniform2f(location, ...values);
}

function resize() {
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const width = Math.max(2, Math.round(canvas.clientWidth * dpr));
  const height = Math.max(2, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    gl?.viewport(0, 0, width, height);
  }
}

function draw() {
  if (!gl || video.readyState < 2) return;
  resize();
  gl.useProgram(program);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, texture);
  try {
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
  } catch {
    return;
  }

  const c = correction();
  uniform('uTex', '1i', 0);
  uniform('uView', '1i', viewMode === 'raw' ? 0 : viewMode === 'eye' ? 1 : 2);
  uniform('uStereo', '1i', stereoCode());
  uniform('uProjection', '1i', projectionCode());
  let eye = selectedEye === 'right' ? 1 : 0;
  if (Boolean(info?.layout_override?.isEyeSwapped ?? info?.isEyeSwapped)) eye = 1 - eye;
  uniform('uEye', '1i', eye);
  uniform('uOrientation', '1i', orientationMode === 'rotate180' ? 1 : orientationMode === 'mirror' ? 2 : orientationMode === 'flipv' ? 3 : 0);
  uniform('uColor', '1i', colorMode === 'original' ? 0 : colorMode === 'corrected' ? 1 : 2);
  uniform('uSplit', '1f', split);
  uniform('uYaw', '1f', yaw);
  uniform('uPitch', '1f', pitch);
  uniform('uViewFov', '1f', viewFov);
  uniform('uSourceFov', '1f', Math.max(60, Number(info?.fov || 180)) * Math.PI / 180);
  uniform('uAspect', '1f', canvas.width / canvas.height);
  uniform('uGain', '3f', ...c.gain);
  uniform('uOffsetRGB', '3f', ...c.offsetRGB);
  uniform('uContrastRGB', '3f', ...c.contrastRGB);
  uniform('uGammaRGB', '3f', ...c.gammaRGB);
  uniform('uSaturationRGB', '3f', ...c.saturationRGB);
  uniform('uTexel', '2f', 1 / Math.max(2, video.videoWidth || 2), 1 / Math.max(2, video.videoHeight || 2));
  uniform('uOffset', '1f', c.offset);
  uniform('uContrast', '1f', c.contrast);
  uniform('uGamma', '1f', c.gamma);
  uniform('uSaturation', '1f', c.saturation);
  uniform('uExposure', '1f', c.exposure);
  uniform('uWhiteTemp', '1f', c.whiteTemp);
  uniform('uWhiteTint', '1f', c.whiteTint);
  uniform('uShadows', '1f', c.shadows);
  uniform('uMidtones', '1f', c.midtones);
  uniform('uHighlights', '1f', c.highlights);
  uniform('uSharpen', '1f', c.sharpen);
  gl.drawArrays(gl.TRIANGLES, 0, 6);
}

function scheduleFrame() {
  if (video.paused || video.ended || frameCallbackPending) return;
  frameCallbackPending = true;
  if (video.requestVideoFrameCallback) {
    video.requestVideoFrameCallback(() => {
      frameCallbackPending = false;
      draw();
      scheduleFrame();
    });
  } else {
    raf = requestAnimationFrame(() => {
      frameCallbackPending = false;
      draw();
      scheduleFrame();
    });
  }
}

function requestDraw() {
  cancelAnimationFrame(raf);
  draw();
}

function updateModeUi() {
  $$('[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === viewMode));
  $$('[data-color]').forEach((button) => button.classList.toggle('active', button.dataset.color === colorMode));
  $$('[data-eye]').forEach((button) => button.classList.toggle('active', button.dataset.eye === selectedEye));
  $$('[data-orientation]').forEach((button) => button.classList.toggle('active', button.dataset.orientation === orientationMode));
  const orientationGroup = $('#orientationGroup');
  if (orientationGroup) orientationGroup.hidden = viewMode === 'raw';
  const compare = colorMode === 'split';
  $('#splitControl').hidden = !compare;
  $('#splitLine').hidden = !compare;
  $('#beforeLabel').hidden = !compare;
  $('#afterLabel').hidden = !compare;
  $('#splitLine').style.left = `${split * 100}%`;
  $('#lookHint').hidden = !(viewMode === 'vr2d' && canVrProject());
  requestDraw();
}

async function emitEvent(event) {
  if (!info) return;
  try {
    await fetch(`/events/${encodeURIComponent(videoId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'HereTune Web Player',
        event,
        time: absoluteTime() * 1000,
        speed: video.playbackRate,
        utc: Date.now(),
        connectionKey: 'web-player',
      }),
    });
  } catch {}
}

function updateTimeline() {
  updateGradeIndicator();
  if (!$('#playerDiagnostics')?.hidden) updateDiagnostics();
  const duration = totalDuration();
  const current = Math.max(0, Math.min(duration || Infinity, absoluteTime()));
  if (Number.isFinite(duration) && duration > 0) {
    $('#timeline').value = Math.round(current / duration * 1000);
    $('#currentTime').textContent = fmtTime(current);
    $('#duration').textContent = fmtTime(duration);
  }
}

function seekAbsolute(seconds, autoplay = !video.paused) {
  const duration = totalDuration();
  const target = Math.max(0, Math.min(Number(seconds || 0), duration || Infinity));
  if (playbackMode === 'original') {
    video.currentTime = target;
    return;
  }
  // Fragmented compatibility streams are generated from the requested seek point.
  // Restarting the stream gives predictable seeking without building giant cached
  // proxy files on disk.
  setVideoSource(playbackMode, target, autoplay);
}

function fail(message) {
  $('#stageLoading').hidden = true;
  $('#stageError').hidden = false;
  $('#stageErrorText').textContent = message;
  $('#directMediaLink').href = mediaUrl();
}

async function init() {
  console.info(`[HereTune Web Player] v${PLAYER_BUILD} script loaded`, { videoId, href: location.href });
  reportPlaybackEvent('player-bootstrap', `build=${PLAYER_BUILD} · href=${location.href}`);
  try {
    info = await getJSON(`/api/videos/${encodeURIComponent(videoId)}`);
    const storedDrag = localStorage.getItem('heretune.reverseVrDrag');
    reverseDrag = storedDrag === null
      ? Boolean(info?.web_player_invert_drag ?? info?.player_settings?.invert_drag)
      : storedDrag === '1';
    const reverseToggle = $('#reverseDragToggle');
    if (reverseToggle) reverseToggle.checked = reverseDrag;
    const lookHint = $('#lookHint');
    if (lookHint) lookHint.textContent = `${reverseDrag ? 'Reverse' : 'Direct'} drag · wheel zoom · R reset`;
    document.title = `${info.title} · HereTune`;
    $('#playerTitle').textContent = info.title;
    $('#playerMeta').textContent = `${info.width || '?'}×${info.height || '?'} · ${info.video_codec || 'video'} · ${info.stereo || 'mono'} / ${info.projection || 'perspective'} · original source`;
    $('#directMediaLink').href = mediaUrl();

    const native = $('#nativePlayerLink');
    native.href = nativeUrl();
    const extension = String(info.media_filename || '').split('.').pop().toLowerCase();
    if (!['mp4', 'webm'].includes(extension)) {
      native.title = 'HereSphere documents automatic built-in-browser interception for direct .mp4/.webm links. Keep HereTune added as a Web API source for other containers.';
    }

    configureViewUiForSource();
    console.info(`[HereTune Web Player] v${PLAYER_BUILD} media metadata loaded`, {
      videoId, stereo: info.stereo, projection: info.projection, codec: info.video_codec, ordinary2D: isOrdinary2D(), textureYFlip: true, correction: correction(),
    });
    reportPlaybackEvent('player-metadata', `build=${PLAYER_BUILD} · stereo=${info.stereo || 'mono'} · projection=${info.projection || 'perspective'} · ordinary2D=${isOrdinary2D()}`);

    try {
      initGL();
    } catch (glError) {
      console.error(`[HereTune Web Player] v${PLAYER_BUILD} WebGL initialization failed`, glError);
      reportPlaybackEvent('webgl-error', String(glError?.message || glError));
      fallbackVideo.hidden = false;
      canvas.hidden = true;
      fallbackVideo.src = mediaUrl();
      fallbackVideo.controls = true;
      $('#stageLoading').hidden = true;
      $('#qualityText').textContent = 'Native browser video fallback · live correction unavailable because WebGL failed';
      return;
    }

    setVideoSource('original', 0, false);
    updateModeUi();
  } catch (error) {
    console.error(`[HereTune Web Player] v${PLAYER_BUILD} initialization failed`, error);
    reportPlaybackEvent('player-init-error', String(error?.message || error));
    fail(`Could not open player metadata: ${error.message}`);
  }
}

function handleMediaFailure(attempt = sourceAttempt, forcedDetail = '') {
  if (attempt !== sourceAttempt || sourceReadyAttempt === attempt || sourceErrorHandledAttempt === attempt) return;
  sourceErrorHandledAttempt = attempt;
  clearTimeout(sourceAttemptTimer);
  sourceSwitching = false;
  const diagnostic = forcedDetail || mediaErrorDescription();
  reportPlaybackEvent('media-error', `attempt=${attempt} · ${diagnostic}`);
  console.error(`[HereTune Web Player] v${PLAYER_BUILD} media failure`, { attempt, playbackMode, diagnostic, readyState: video.readyState, networkState: video.networkState, currentSrc: video.currentSrc });
  if (playbackMode === 'original') {
    setVideoSource(compatibilityFirstStep(), 0, false);
    return;
  }
  if (playbackMode === 'remux') {
    setVideoSource('transcode', compatibilityStart, pendingAutoplay);
    return;
  }
  fail(`Chrome rejected the original, remux, and compatibility stream. Last browser error: ${diagnostic}. Check the HereTune console for lines beginning “[HereTune Web]”; they include the source attempt and FFmpeg exit/error details.`);
}

video.addEventListener('loadedmetadata', () => {
  console.info(`[HereTune Web Player] v${PLAYER_BUILD} playback source ready`, { attempt: sourceAttempt, playbackMode, width: video.videoWidth, height: video.videoHeight, currentSrc: video.currentSrc });
  sourceReadyAttempt = sourceAttempt;
  sourceSwitching = false;
  clearTimeout(sourceAttemptTimer);
  reportPlaybackEvent('loadedmetadata', `attempt=${sourceAttempt} · ${video.videoWidth}x${video.videoHeight}`);
  $('#stageLoading').hidden = true;
  $('#stageError').hidden = true;
  if (playbackMode === 'original') {
    $('#qualityText').textContent = `Original ${video.videoWidth}×${video.videoHeight} source · HTTP Range · zero transcode`;
    $('#sourcePillLabel').textContent = 'ORIGINAL';
  } else if (playbackMode === 'remux') {
    $('#qualityText').textContent = `Original video quality · browser MP4 remux · video not re-encoded`;
    $('#sourcePillLabel').textContent = 'ORIGINAL VIDEO';
  } else {
    $('#qualityText').textContent = `Browser-compatible high quality · H.264/AAC · up to 3840px wide · source untouched`;
    $('#sourcePillLabel').textContent = 'COMPATIBILITY';
  }
  resize();
  draw();
  updateTimeline();
  if (!openedEventSent) {
    openedEventSent = true;
    emitEvent(0);
  }
  if (pendingAutoplay) {
    pendingAutoplay = false;
    video.play().catch(() => {});
  }
});
video.addEventListener('error', () => {
  const attempt = sourceAttempt;
  if (sourceSwitching) {
    // Defer errors emitted during src replacement so a stale event from the old
    // source cannot consume the fallback ladder. If this attempt genuinely failed,
    // it will still be current after the switching window.
    setTimeout(() => handleMediaFailure(attempt), 500);
    return;
  }
  handleMediaFailure(attempt);
});
video.addEventListener('play', () => { $('#playButton').textContent = '❚❚'; emitEvent(1); scheduleFrame(); });
video.addEventListener('pause', () => { $('#playButton').textContent = '▶'; emitEvent(2); requestDraw(); });
video.addEventListener('timeupdate', updateTimeline);
video.addEventListener('ended', () => emitEvent(3));

$('#playButton').addEventListener('click', () => video.paused ? video.play() : video.pause());
$('#rewindButton').addEventListener('click', () => seekAbsolute(absoluteTime() - 10));
$('#forwardButton').addEventListener('click', () => seekAbsolute(absoluteTime() + 10));
$('#timeline').addEventListener('input', (event) => {
  const duration = totalDuration();
  if (!duration) return;
  const target = Number(event.target.value) / 1000 * duration;
  $('#currentTime').textContent = fmtTime(target);
  if (playbackMode === 'original') video.currentTime = target;
});
$('#timeline').addEventListener('change', (event) => {
  if (playbackMode === 'original') return;
  const duration = totalDuration();
  if (duration) seekAbsolute(Number(event.target.value) / 1000 * duration);
});
$('#volume').addEventListener('input', (event) => { video.volume = Number(event.target.value); });
$('#fullscreenButton').addEventListener('click', () => stage.requestFullscreen?.());
$('#resetCameraButton')?.addEventListener('click', resetCamera);
$('#diagnosticsButton')?.addEventListener('click', () => toggleDiagnostics());
$('#closeDiagnosticsButton')?.addEventListener('click', () => toggleDiagnostics(false));

$$('[data-view]').forEach((button) => button.addEventListener('click', () => {
  viewMode = button.dataset.view;
  if (viewMode === 'vr2d' && !canVrProject()) viewMode = 'eye';
  updateModeUi();
}));
$$('[data-color]').forEach((button) => button.addEventListener('click', () => { colorMode = button.dataset.color; updateModeUi(); }));
$$('[data-eye]').forEach((button) => button.addEventListener('click', () => { selectedEye = button.dataset.eye; updateModeUi(); }));
$$('[data-orientation]').forEach((button) => button.addEventListener('click', () => { orientationMode = button.dataset.orientation; updateModeUi(); }));
$('#splitRange').addEventListener('input', (event) => { split = Number(event.target.value) / 100; updateModeUi(); });

canvas.addEventListener('pointerdown', (event) => {
  if (viewMode !== 'vr2d' || !canVrProject()) return;
  dragging = true;
  lastX = event.clientX;
  lastY = event.clientY;
  canvas.setPointerCapture?.(event.pointerId);
});
canvas.addEventListener('pointermove', (event) => {
  if (!dragging) return;
  const dx = event.clientX - lastX;
  const dy = event.clientY - lastY;
  lastX = event.clientX;
  lastY = event.clientY;
  const direction = reverseDrag ? -1 : 1;
  yaw += dx * .004 * direction;
  pitch = Math.max(-1.35, Math.min(1.35, pitch + dy * .004 * direction));
  requestDraw();
});
canvas.addEventListener('pointerup', () => { dragging = false; });
canvas.addEventListener('pointercancel', () => { dragging = false; });
canvas.addEventListener('wheel', (event) => {
  if (viewMode !== 'vr2d' || !canVrProject()) return;
  event.preventDefault();
  viewFov = Math.max(35 * Math.PI / 180, Math.min(120 * Math.PI / 180, viewFov + Math.sign(event.deltaY) * .06));
  requestDraw();
}, { passive: false });

$('#reverseDragToggle')?.addEventListener('change', (event) => {
  reverseDrag = Boolean(event.target.checked);
  localStorage.setItem('heretune.reverseVrDrag', reverseDrag ? '1' : '0');
  $('#lookHint').textContent = `${reverseDrag ? 'Reverse' : 'Direct'} drag · wheel zoom · R reset`;
});

document.addEventListener('keydown', (event) => {
  if (event.target && ['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName)) return;
  const key = event.key.toLowerCase();
  if (event.code === 'Space') { event.preventDefault(); video.paused ? video.play() : video.pause(); return; }
  if (event.key === 'ArrowLeft') { event.preventDefault(); seekAbsolute(absoluteTime() - 10); return; }
  if (event.key === 'ArrowRight') { event.preventDefault(); seekAbsolute(absoluteTime() + 10); return; }
  if (key === 'r') { resetCamera(); return; }
  if (key === 'd') { toggleDiagnostics(); return; }
  if (key === 'o') { colorMode = 'original'; updateModeUi(); return; }
  if (key === 'c') { colorMode = 'corrected'; updateModeUi(); return; }
  if (key === 'b') { colorMode = 'split'; updateModeUi(); return; }
  if (key === 'f') stage.requestFullscreen?.();
});

window.addEventListener('resize', requestDraw);
window.addEventListener('beforeunload', () => {
  const body = JSON.stringify({
    username: 'HereTune Web Player',
    event: 3,
    time: absoluteTime() * 1000,
    speed: video.playbackRate,
    utc: Date.now(),
    connectionKey: 'web-player',
  });
  navigator.sendBeacon?.(`/events/${encodeURIComponent(videoId)}`, new Blob([body], { type: 'application/json' }));
});

updateModeUi();
init();
