const state = {
  videos: [],
  total: 0,
  page: 1,
  limit: 50,
  selectedId: null,
  loading: false,
  query: "",
  status: "",
  favorite: "all",
  rating: "all",
  watch: "all",
  duplicates: "all",
  order: "title",
  statusPayload: null,
  configPayload: null,
  reloadRequested: false,
  lastStatusSignature: "",
  liveConnected: false,
  liveSource: null,
  selectedIds: new Set(),
  selectionAllMatching: false,
  setupStatus: null,
  setupStep: 0,
  setupPreset: "balanced",
  setupLibraryRoots: [""],
  settingsLibraryRoots: [],
  tourIndex: 0,
  lastSidecarSyncToast: "",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

// Thumbnail calibration is part of the unified diagonal visual workbench.

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtNumber(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, Number(value)));
}

function signed(value, digits = 3) {
  const numeric = Number(value || 0);
  return `${numeric >= 0 ? "+" : ""}${numeric.toFixed(digits)}`;
}

function setStatText(selector, value) {
  const node = $(selector);
  const text = String(value);
  if (node && node.textContent !== text) {
    node.textContent = text;
    node.classList.remove("stat-pop");
    void node.offsetWidth;
    node.classList.add("stat-pop");
  }
}

function fmtBytes(value) {
  const size = Number(value || 0);
  if (!size) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  return `${(size / 1024 ** index).toFixed(index > 2 ? 2 : 1)} ${units[index]}`;
}

function fmtDuration(milliseconds) {
  const total = Math.round(Number(milliseconds || 0) / 1000);
  if (!total) return "unknown duration";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function fmtShortSeconds(value) {
  const seconds = Math.max(0, Math.round(Number(value || 0)));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remain = seconds % 60;
  if (minutes < 60) return `${minutes}m ${String(remain).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function fmtClockSeconds(value) {
  const seconds = Math.max(0, Number(value || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remain = Math.floor(seconds % 60);
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remain).padStart(2, "0")}`
    : `${minutes}:${String(remain).padStart(2, "0")}`;
}

function fmtVector(value, digits = 3, fallback = "—") {
  if (!Array.isArray(value) || !value.length) return fallback;
  return value.map((part) => Number(part || 0).toFixed(digits)).join(" / ");
}

function fmtRgb(c, prefix, neutral = 1, digits = 3) {
  const values = ["r", "g", "b"].map((channel) => Number(c?.[`${prefix}_${channel}`] ?? neutral));
  return values.map((value) => value.toFixed(digits)).join(" / ");
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toasts").append(node);
  window.setTimeout(() => node.remove(), 5000);
}

const ANALYSIS_STAGES = [
  ["metadata", "Metadata"],
  ["scene-probe", "Scene scan"],
  ["boundary-refinement", "Refine"],
  ["full-sampling", "Full samples"],
  ["frame-analysis", "Analyze"],
  ["sample-rejection", "Reject"],
  ["audio-analysis", "Audio"],
  ["native-calibration", "Calibrate"],
  ["finalizing", "HSP"],
];

function normalizedStage(stage) {
  const value = String(stage || "metadata");
  if (["starting", "analyzing", "analysis"].includes(value)) return "frame-analysis";
  if (["complete", "ready"].includes(value)) return "finalizing";
  return value;
}

function renderPipelineStages(stage) {
  const node = $("#pipelineStages");
  if (!node) return;
  const current = normalizedStage(stage);
  const currentIndex = Math.max(0, ANALYSIS_STAGES.findIndex(([key]) => key === current));
  node.innerHTML = ANALYSIS_STAGES.map(([key, label], index) =>
    `<span class="pipeline-stage ${index < currentIndex ? "done" : index === currentIndex ? "active" : "pending"}" title="${escapeHtml(label)}"><span class="pipeline-stage-mark" aria-hidden="true">${index < currentIndex ? "✓" : index === currentIndex ? "●" : "○"}</span><b>${escapeHtml(label)}</b></span>`
  ).join("");
}

const SIDECAR_STAGES = [["discover", "Discover"], ["write", "Validate + write"], ["done", "Done"]];

function renderSidecarStages(stage) {
  const node = $("#pipelineStages");
  if (!node) return;
  const current = String(stage || "write");
  const currentIndex = Math.max(0, SIDECAR_STAGES.findIndex(([key]) => key === current));
  node.innerHTML = SIDECAR_STAGES.map(([key, label], index) =>
    `<span class="pipeline-stage ${index < currentIndex ? "done" : index === currentIndex ? "active" : "pending"}" title="${escapeHtml(label)}"><span class="pipeline-stage-mark" aria-hidden="true">${index < currentIndex ? "✓" : index === currentIndex ? "●" : "○"}</span><b>${escapeHtml(label)}</b></span>`
  ).join("");
}

function sidecarProgressSuffix(sync) {
  if (!sync?.running) return "";
  const processed = Number(sync.processed || 0);
  const total = Number(sync.total || 0);
  return total > 0 ? ` · HSP sidecars ${fmtNumber(processed)}/${fmtNumber(total)}` : " · HSP sidecars preparing";
}


async function fetchJSON(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, { ...options, headers });
  const isJson = response.headers.get("content-type")?.includes("application/json");
  const payload = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" ? payload.detail || JSON.stringify(payload) : payload;
    const error = new Error(detail || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function endpointUrl() {
  return `${window.location.protocol}//${window.location.host}/heresphere`;
}

async function copyText(value) {
  const text = String(value || "");

  // Do the synchronous copy first while this call is still inside the original
  // click activation. In some Chromium builds, awaiting navigator.clipboard first
  // consumes that activation before a fallback gets a chance to run.
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "readonly");
  helper.setAttribute("aria-hidden", "true");
  helper.style.position = "fixed";
  helper.style.top = "0";
  helper.style.left = "-9999px";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.focus({ preventScroll: true });
  helper.select();
  helper.setSelectionRange(0, helper.value.length);
  try {
    if (document.execCommand?.("copy")) return true;
  } catch (error) {
    console.warn("Synchronous clipboard copy failed.", error);
  } finally {
    helper.remove();
  }

  // Modern Clipboard API is the second path, not the first.
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      console.warn("Clipboard API copy failed.", error);
    }
  }
  return false;
}

async function refreshStatus() {
  try {
    const data = await fetchJSON("/api/status");
    state.statusPayload = data;
    $("#healthPulse").className = "pulse ok";
    $("#connectionText").textContent = `HereTune ${data.version || ""} online`.trim();
    if ($("#footerVersion")) $("#footerVersion").textContent = data.version ? `v${data.version}` : "v—";
    const resolvedMode = data.analysis?.resolved_mode || "cpu";
    const cuda = data.analysis?.cuda;
    $("#heresphereUrl").textContent = endpointUrl();

    const stats = data.stats || {};
    setStatText("#statTotal", fmtNumber(stats.total));
    setStatText("#statReady", fmtNumber(stats.ready));
    const queuedCount = Number(stats.pending || 0) + Number(stats.analyzing || 0);
    setStatText("#statPending", fmtNumber(queuedCount));
    setStatText("#statConfidence", stats.ready ? `${Math.round((stats.average_confidence || 0) * 100)}%` : "—");

    const queueEta = data.queue_eta || {};
    const etaText = Number(queueEta.remaining_seconds || 0) > 0 ? fmtShortSeconds(queueEta.remaining_seconds) : (queuedCount ? "calculating" : "clear");
    $("#statQueueDetail").textContent = queuedCount
      ? `${etaText} ETA${queueEta.finish_at ? ` · ~${new Date(queueEta.finish_at).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})}` : ""}`
      : "nothing waiting";

    const plan = data.analysis?.worker_plan || {};
    const telemetry = data.analysis?.gpu_telemetry;
    const workerBits = [];
    if (plan.gpu_workers) workerBits.push(`${plan.gpu_workers} GPU`);
    if (plan.cpu_workers) workerBits.push(`${plan.cpu_workers} CPU`);
    if (plan.nas_latency_ms !== null && plan.nas_latency_ms !== undefined) workerBits.push(`NAS ${Math.round(plan.nas_latency_ms)}ms`);
    if (resolvedMode === "hybrid" && cuda?.usable) {
      $("#accelerationText").textContent = `Hybrid · ${workerBits.join(" / ") || "adaptive workers"}${plan.adaptive ? " · adaptive" : ""}`;
    } else if (resolvedMode === "gpu" && cuda?.usable) {
      $("#accelerationText").textContent = `GPU sampling · ${workerBits.join(" / ") || "ready"}${plan.adaptive ? " · adaptive" : ""}`;
    } else if (cuda && !cuda.usable && data.analysis?.requested_mode !== "cpu") {
      $("#accelerationText").textContent = `CPU active · CUDA unavailable${plan.cpu_workers ? ` · ${plan.cpu_workers} workers` : ""}`;
    } else {
      $("#accelerationText").textContent = `CPU sampling + correction${plan.cpu_workers ? ` · ${plan.cpu_workers} workers` : ""}${plan.adaptive ? " · adaptive" : ""}`;
    }

    // Refresh the actual table immediately when queue state changes. This makes
    // a video appear under the Ready filter as soon as its analysis completes,
    // rather than waiting for the slower fallback library poll.
    const signature = JSON.stringify([
      stats.ready || 0, stats.pending || 0, stats.analyzing || 0,
      stats.error || 0, stats.skipped || 0,
    ]);
    if (state.lastStatusSignature && signature !== state.lastStatusSignature) {
      loadVideos();
    }
    state.lastStatusSignature = signature;

    const paused = Boolean(data.work?.paused);
    const pauseButton = $("#pauseWorkButton");
    pauseButton.textContent = paused ? "Resume work" : "Pause work";
    pauseButton.classList.toggle("paused", paused);
    pauseButton.title = paused
      ? "Resume automatic indexing, metadata probing, and analysis"
      : "Pause new indexing/probing/analysis jobs; the current job finishes safely";

    const lastFailure = data.last_analysis_failure || null;
    if (lastFailure?.occurred_at) {
      const failureKey = `${lastFailure.video_id || ""}|${lastFailure.occurred_at}`;
      if (failureKey !== state.lastFailureKey) {
        state.lastFailureKey = failureKey;
        const failureReason = String(lastFailure.error || "unknown error");
        const conciseReason = failureReason.length > 260 ? `${failureReason.slice(0, 257)}…` : failureReason;
        toast(`Analysis failed: ${lastFailure.title || "video"} — ${conciseReason}. Queue continuing.`, "error");
      }
    }

    const active = data.active || [];
    const sidecarSync = data.sidecar_sync || {};
    const strip = $("#workStrip");
    const progressBar = $("#workProgressBar");
    const progressFill = progressBar.querySelector("i");
    if (active.length) {
      const item = active[0];
      strip.hidden = false;
      $("#workTitle").textContent = item.title || "Analyzing";
      const completed = Number(item.completed || 0);
      const total = Number(item.total || 0);
      const stage = normalizedStage(item.stage || "frame-analysis");
      renderPipelineStages(stage);
      if (total > 0 && ["scene-probe", "boundary-refinement", "full-sampling", "frame-analysis"].includes(stage)) {
        const percent = Math.max(0, Math.min(100, Math.round(completed / total * 100)));
        progressBar.classList.add("determinate");
        progressFill.style.width = `${percent}%`;
        const elapsed = Math.max(0, Number(item.elapsed_seconds || 0));
        let etaSeconds = item.eta_seconds;
        // Defensive client-side fallback for mixed-version workers: if a worker
        // supplies counts + elapsed time but no ETA, derive the remaining time
        // from the observed rate instead of displaying "calculating" forever.
        if ((etaSeconds === null || etaSeconds === undefined) && completed > 0 && total > completed && elapsed > 0) {
          etaSeconds = (elapsed / completed) * (total - completed);
        }
        const eta = etaSeconds === null || etaSeconds === undefined ? "calculating ETA…" : `ETA ${fmtShortSeconds(etaSeconds)}`;
        const stalled = Number(item.stalled_seconds || 0);
        const waiting = stalled >= 8 ? ` · waiting on decoder ${fmtShortSeconds(stalled)}` : "";
        $("#workDetail").textContent = `${completed} / ${total} frames · ${percent}% · elapsed ${fmtShortSeconds(elapsed)} · ${eta}${waiting}`;
      } else {
        progressBar.classList.remove("determinate");
        progressFill.style.width = "42%";
        const labels = {
          metadata: "Reading video metadata and projection hints…",
          thumbnail: "Creating monitor-friendly browse preview…",
          "scene-probe": "Scanning the timeline for sustained grading-state changes…",
          "boundary-refinement": "Refining candidate scene boundaries…",
          "full-sampling": "Collecting full 960px analysis samples…",
          "frame-analysis": "Measuring color, tone, skin/neutral evidence, and detail…",
          "sample-rejection": "Rejecting fades, title-like frames, and near-duplicates…",
          "audio-analysis": "Analyzing loudness and A/V timing evidence…",
          "native-calibration": "Solving VR geometry and native calibration metadata…",
          finalizing: "Building and validating the native HSP…",
          ready: "Analysis complete.",
        };
        const stageElapsed = Math.max(0, Number(item.elapsed_seconds || 0));
        $("#workDetail").textContent = `${labels[stage] || "Preparing analysis…"}${stageElapsed > 0 ? ` · elapsed ${fmtShortSeconds(stageElapsed)}` : ""}`;
      }
      if (paused) $("#workDetail").textContent += " · pause requested; this video will finish first";
      $("#workDetail").textContent += sidecarProgressSuffix(sidecarSync);
    } else if (sidecarSync?.running) {
      strip.hidden = false;
      const processed = Number(sidecarSync.processed || 0);
      const total = Number(sidecarSync.total || 0);
      const percent = total > 0 ? Math.max(0, Math.min(100, Math.round(processed / total * 100))) : 0;
      progressBar.classList.toggle("determinate", total > 0);
      progressFill.style.width = total > 0 ? `${percent}%` : "42%";
      $("#workTitle").textContent = "Syncing portable HSP sidecars";
      renderSidecarStages(sidecarSync.stage || "write");
      const counts = [
        `${fmtNumber(processed)} / ${fmtNumber(total)} · ${percent}%`,
        `${fmtNumber(sidecarSync.written || 0)} created/updated`,
        `${fmtNumber(sidecarSync.unchanged || 0)} current`,
      ];
      if (Number(sidecarSync.conflicts || 0)) counts.push(`${fmtNumber(sidecarSync.conflicts)} conflicts`);
      if (Number(sidecarSync.deferred || 0)) counts.push(`${fmtNumber(sidecarSync.deferred)} deferred`);
      if (Number(sidecarSync.errors || 0)) counts.push(`${fmtNumber(sidecarSync.errors)} errors`);
      const eta = sidecarSync.eta_seconds === null || sidecarSync.eta_seconds === undefined
        ? "calculating ETA…"
        : `ETA ${fmtShortSeconds(sidecarSync.eta_seconds)}`;
      counts.push(eta);
      $("#workDetail").textContent = counts.join(" · ");
    } else if (data.scan?.running) {
      strip.hidden = false;
      progressBar.classList.remove("determinate");
      progressFill.style.width = "42%";
      $("#workTitle").textContent = "Scanning the library";
      $("#workDetail").textContent = "Comparing NAS files with the local index…";
      $("#pipelineStages").innerHTML = "";
    } else if (paused) {
      strip.hidden = false;
      progressBar.classList.add("determinate");
      progressFill.style.width = "100%";
      $("#workTitle").textContent = "Indexing & analysis paused";
      $("#workDetail").textContent = "Queue order is preserved. Resume when you want the pixel bureaucracy to continue.";
      $("#pipelineStages").innerHTML = "";
    } else {
      strip.hidden = true;
      $("#pipelineStages").innerHTML = "";
    }
  } catch (error) {
    $("#healthPulse").className = "pulse error";
    $("#connectionText").textContent = "Server unavailable";
    const strip = $("#workStrip");
    const progressBar = $("#workProgressBar");
    if (strip && progressBar) {
      strip.hidden = false;
      progressBar.classList.remove("determinate");
      const fill = progressBar.querySelector("i");
      if (fill) fill.style.width = "42%";
      $("#workTitle").textContent = "HereTune server connection lost";
      $("#workDetail").textContent = "Progress is unknown. Check the HereTune console; this page will reconnect automatically.";
    }
  }
}

function statusBadge(status) {
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span>`;
}

function correctionCell(video) {
  const c = video.correction || {};
  if (!Object.keys(c).length) {
    const priority = ["pending", "error"].includes(video.status)
      ? `<button type="button" class="queue-priority-button correction-priority ${video.manual_priority ? "active" : ""}" data-priority-video="${escapeHtml(video.id)}"><span>${video.manual_priority ? "✓" : "↑"}</span>${video.manual_priority ? `Prioritized${video.priority_rank ? ` #${escapeHtml(video.priority_rank)}` : ""}` : "Prioritize"}</button>`
      : "";
    return `<div class="not-analyzed-cell"><span class="profile-label original">Not analyzed</span>${priority}</div>`;
  }
  const scenes = Math.max(1, Number(c.scene_count || (Array.isArray(c.scene_keyframes) ? c.scene_keyframes.length : 0) || 1));
  const channelConfidence = Number(c.per_channel_confidence || 0);
  const hasChannelTrim = ["offset", "contrast", "gamma", "saturation"].some((prefix) => {
    const neutral = prefix === "offset" ? 0 : 1;
    return ["r", "g", "b"].some((channel) => Math.abs(Number(c[`${prefix}_${channel}`] ?? neutral) - neutral) > 0.0005);
  });
  return `<div class="correction-summary">
    <div class="correction-summary-primary">
      <span><small>WB</small><b>${Number(c.gain_r ?? 1).toFixed(3)}/${Number(c.gain_g ?? 1).toFixed(3)}/${Number(c.gain_b ?? 1).toFixed(3)}</b></span>
      <span><small>TEMP / TINT</small><b>${Math.round(Number(c.white_temp ?? 6500))}K · ${signed(c.white_tint || 0, 3)}</b></span>
    </div>
    <div class="correction-summary-chips">
      <span>Exp <b>${signed(c.exposure_bias || 0, 3)}</b></span>
      <span>Con <b>${Number(c.contrast ?? 1).toFixed(3)}</b></span>
      <span>Sat <b>${Number(c.saturation ?? 1).toFixed(3)}</b></span>
      <span>Gam <b>${Number(c.gamma ?? 1).toFixed(3)}</b></span>
      <span>Sharp <b>${Number(c.sharpen ?? 2).toFixed(1)}</b></span>
    </div>
    <div class="correction-summary-badges">
      ${scenes > 1 ? `<span class="summary-badge scene">${scenes} scene grades</span>` : '<span class="summary-badge">Whole-video grade</span>'}
      ${hasChannelTrim ? `<span class="summary-badge channel">RGB trim ${Math.round(channelConfidence * 100)}%</span>` : ""}
      ${Math.abs(Number(c.audio_volume_multiplier ?? 1) - 1) > .005 ? `<span class="summary-badge audio">Audio ${Number(c.audio_volume_multiplier).toFixed(2)}×</span>` : ""}
    </div>
  </div>`;
}

function frameSourceText(a) {
  if (!a?.execution_mode) return "—";
  const gpu = Number(a.gpu_samples || 0);
  const compat = Number(a.gpu_compat_samples || 0);
  const cpu = Number(a.cpu_samples || 0);
  const mode = String(a.execution_mode || "cpu").toUpperCase();
  if (gpu > 0 && cpu > 0) return `${mode} · ${gpu} GPU + ${cpu} CPU samples`;
  if (gpu > 0 && compat > 0) return `${mode} · ${gpu} GPU samples · NVDEC compatibility path`;
  if (gpu > 0) return `${mode} · ${gpu} GPU samples`;
  if (cpu > 0 && a.gpu_preflight_fallback) return `CPU · NVDEC fallback for this source`;
  if (cpu > 0) return `${mode} · ${cpu} CPU samples`;
  return mode;
}

function ratingStars(value) {
  const rating = value === null || value === undefined ? 0 : clamp(value, 0, 5);
  const label = value === null || value === undefined ? "Unrated" : `${rating.toFixed(1)} / 5`;
  return `<span class="stars" role="img" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">${[1,2,3,4,5].map((n) => {
    const fill = clamp((rating - (n - 1)) * 100, 0, 100);
    return `<span class="star-glyph" style="--star-fill:${fill}%" aria-hidden="true">★</span>`;
  }).join("")}</span>`;
}

function ratingPicker(value) {
  const rating = value === null || value === undefined ? 0 : clamp(value, 0, 5);
  return `<div class="star-editor" role="radiogroup" aria-label="Star rating in half-star increments" data-current-rating="${rating.toFixed(1)}">${[1,2,3,4,5].map((n) => {
    const fill = clamp((rating - (n - 1)) * 100, 0, 100);
    const half = n - 0.5;
    return `<span class="rating-star" data-rating-star="${n}" style="--star-fill:${fill}%;--star-preview-fill:${fill}%">
      <span class="rating-star-glyph" aria-hidden="true">★</span>
      <button type="button" class="rating-half rating-half-left" data-star-rating="${half.toFixed(1)}" role="radio" aria-checked="${rating === half}" aria-label="Set rating to ${half.toFixed(1)} out of 5" title="${half.toFixed(1)} stars"></button>
      <button type="button" class="rating-half rating-half-right" data-star-rating="${n.toFixed(1)}" role="radio" aria-checked="${rating === n}" aria-label="Set rating to ${n.toFixed(1)} out of 5" title="${n.toFixed(1)} stars"></button>
    </span>`;
  }).join("")}</div>`;
}

function initializeRatingPicker(video) {
  const editor = $(".star-editor");
  if (!editor) return;
  const current = video.rating === null || video.rating === undefined ? 0 : clamp(Number(video.rating), 0, 5);
  const valueLabel = $("#ratingValueLabel");
  const stars = $$(".rating-star", editor);

  const render = (rating, preview = false) => {
    stars.forEach((star, index) => {
      const fill = clamp((Number(rating) - index) * 100, 0, 100);
      star.style.setProperty("--star-preview-fill", `${fill}%`);
    });
    editor.classList.toggle("is-preview", preview);
    if (valueLabel) valueLabel.textContent = rating > 0 ? `${Number(rating).toFixed(1)} / 5` : "Unrated";
  };
  const restore = () => render(current, false);

  $$("[data-star-rating]", editor).forEach((button) => {
    const preview = () => render(Number(button.dataset.starRating), true);
    button.addEventListener("pointerenter", preview);
    button.addEventListener("focus", preview);
  });
  editor.addEventListener("pointerleave", restore);
  editor.addEventListener("focusout", (event) => {
    if (!editor.contains(event.relatedTarget)) restore();
  });
  restore();
}

function libraryStateCell(video) {
  const favorite = Boolean(video.is_favorite);
  return `<div class="library-state-cell">
    <span class="favorite-state ${favorite ? "on" : ""}">${favorite ? "♥ Favorite" : "♡ Not favorite"}</span>
    ${ratingStars(video.rating)}
    <small>${video.rating === null || video.rating === undefined ? "unrated" : `${Number(video.rating).toFixed(1)} / 5`}</small>
  </div>`;
}

function updateSelectionUi() {
  const count = state.selectedIds.size;
  const bar = $("#selectionBar");
  if (bar) bar.hidden = count === 0;
  if ($("#selectionCount")) $("#selectionCount").textContent = `${fmtNumber(count)} selected`;
  if ($("#selectionScope")) $("#selectionScope").textContent = state.selectionAllMatching ? `All ${fmtNumber(state.total)} matching videos` : "Selected rows";
  const visibleIds = state.videos.map((video) => String(video.id));
  const visibleSelected = visibleIds.filter((id) => state.selectedIds.has(id)).length;
  const allVisible = visibleIds.length > 0 && visibleSelected === visibleIds.length;
  const someVisible = visibleSelected > 0 && !allVisible;
  const head = $("#selectVisibleCheckbox");
  if (head) { head.checked = allVisible; head.indeterminate = someVisible; }
  $$('[data-select-video]').forEach((box) => { box.checked = state.selectedIds.has(box.dataset.selectVideo); });
}

function clearSelection() {
  state.selectedIds.clear();
  state.selectionAllMatching = false;
  updateSelectionUi();
}

function currentFilterParams() {
  const params = new URLSearchParams({ order: state.order });
  if (state.query) params.set("q", state.query);
  if (state.status) params.set("status", state.status);
  if (state.favorite) params.set("favorite", state.favorite);
  if (state.rating) params.set("rating", state.rating);
  if (state.watch) params.set("watch", state.watch);
  if (state.duplicates) params.set("duplicates", state.duplicates);
  return params;
}

async function selectAllMatching() {
  const button = $("#selectAllButton");
  button.disabled = true;
  try {
    const data = await fetchJSON(`/api/video-ids?${currentFilterParams()}`);
    state.selectedIds = new Set((data.ids || []).map(String));
    state.selectionAllMatching = true;
    updateSelectionUi();
    toast(`Selected ${fmtNumber(state.selectedIds.size)} matching video(s).`, "success");
  } catch (error) {
    toast(`Could not select matching videos: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function prioritizeSelection() {
  const ids = [...state.selectedIds];
  if (!ids.length) return;
  const button = $("#prioritizeSelectionButton");
  button.disabled = true;
  try {
    const result = await fetchJSON("/api/videos/bulk-priority", {
      method: "POST",
      body: JSON.stringify({ ids, priority: true, position: "back" }),
    });
    const skipped = Number(result.skipped || 0);
    toast(`Added ${fmtNumber(result.changed || 0)} video(s) to the end of the priority queue${skipped ? ` · ${fmtNumber(skipped)} ready/analyzing and skipped` : ""}.`, "success");
    clearSelection();
    await Promise.all([loadVideos(), refreshStatus()]);
  } catch (error) {
    toast(`Could not prioritize selection: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

function renderRows() {
  const body = $("#videoRows");
  body.innerHTML = state.videos.map((video) => {
    const confidence = Math.round(Number(video.confidence || 0) * 100);
    const thumbnail = video.thumbnail_path
      ? `/thumb/${encodeURIComponent(video.id)}.jpg?rev=${encodeURIComponent(video.thumbnail_revision || "7")}`
      : "/placeholder.svg";
    const profile = video.profile_mode || "auto";
    const resolution = video.width && video.height ? `${video.width}×${video.height}` : "unprobed";
    const engine = frameSourceText(video.analysis);
    return `<tr data-video-id="${escapeHtml(video.id)}" class="video-row ${state.selectedIds.has(String(video.id)) ? "selected-row" : ""}">
      <td class="select-column select-cell" data-col="select"><input type="checkbox" data-select-video="${escapeHtml(video.id)}" aria-label="Select ${escapeHtml(video.title)}" ${state.selectedIds.has(String(video.id)) ? "checked" : ""}></td>
      <td class="video-column" data-col="video"><div class="video-cell"><div class="video-thumb-wrap"><img class="video-thumb" src="${thumbnail}" alt="" loading="lazy"></div><div class="video-title"><strong title="${escapeHtml(video.title)}">${video.is_favorite ? '<span class="favorite-mark">♥</span>' : ""}${escapeHtml(video.title)}</strong><span title="${escapeHtml(video.rel_path)}">${escapeHtml(video.folder || "Library root")} · ${resolution} · ${fmtDuration(video.duration_ms)} · ${escapeHtml(engine)}${video.watch?.last_watched ? ` · watched ${Math.round((video.watch.progress || 0) * 100)}%` : ""}${video.duplicate_group ? " · duplicate" : ""}</span></div></div></td>
      <td class="status-column" data-col="status" data-label="Status"><div class="status-stack">${statusBadge(video.status)}${video.manual_priority ? `<span class="priority-badge">NEXT #${escapeHtml(video.priority_rank || "")}</span>` : ""}${video.status === "error" && video.error ? `<small class="row-error-reason" title="${escapeHtml(video.error)}">${escapeHtml(video.error)}</small>` : ""}</div></td>
      <td class="profile-column" data-col="profile" data-label="Profile"><span class="profile-label ${escapeHtml(profile)}">${escapeHtml(profile)}</span><small class="muted profile-hsp-state">${escapeHtml(video.hsp_status || "missing")}</small></td>
      <td class="library-column" data-col="library" data-label="Library">${libraryStateCell(video)}</td>
      <td class="correction-column" data-col="corrections" data-label="Corrections">${correctionCell(video)}</td>
      <td class="confidence-column" data-col="confidence" data-label="Confidence"><div class="confidence-bar" style="--confidence:${confidence}%"><i></i><span>${video.status === "ready" ? `${confidence}%` : "—"}</span></div></td>
      <td class="action-column" data-col="action"><button class="row-action" title="Open details" aria-label="Open ${escapeHtml(video.title)} details">›</button></td>
    </tr>`;
  }).join("");
  $$("#videoRows tr").forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("input,button,a,select,label")) return;
    openVideo(row.dataset.videoId);
  }));
  $$("#videoRows .row-action").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = button.closest("tr[data-video-id]");
    if (row?.dataset.videoId) openVideo(row.dataset.videoId);
  }));
  $$('[data-select-video]').forEach((box) => box.addEventListener("click", (event) => event.stopPropagation()));
  $$('[data-select-video]').forEach((box) => box.addEventListener("change", () => {
    const id = String(box.dataset.selectVideo);
    if (box.checked) state.selectedIds.add(id); else state.selectedIds.delete(id);
    box.closest("tr")?.classList.toggle("selected-row", box.checked);
    state.selectionAllMatching = false;
    updateSelectionUi();
  }));
  $$('[data-priority-video]').forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const video = state.videos.find((item) => item.id === button.dataset.priorityVideo);
    setPriority(button.dataset.priorityVideo, !Boolean(video?.manual_priority));
  }));
  $("#emptyState").hidden = state.videos.length > 0;
  renderPagination();
  updateSelectionUi();
  window.requestAnimationFrame(refreshFloatingTableScrollbar);
}

function renderPagination() {
  const total = Math.max(0, Number(state.total || 0));
  const pageCount = Math.max(1, Math.ceil(total / state.limit));
  state.page = clamp(state.page, 1, pageCount);
  const start = total ? ((state.page - 1) * state.limit) + 1 : 0;
  const end = total ? Math.min(total, start + state.videos.length - 1) : 0;
  $("#resultCount").textContent = total
    ? `${fmtNumber(start)}–${fmtNumber(end)} of ${fmtNumber(total)} videos`
    : "0 videos";
  $("#pageStatus").textContent = `Page ${fmtNumber(state.page)} of ${fmtNumber(pageCount)}`;
  $("#pageStatusTop").textContent = `${fmtNumber(state.page)} / ${fmtNumber(pageCount)}`;
  $("#pageFirstButton").disabled = state.page <= 1;
  $("#pagePrevButton").disabled = state.page <= 1;
  $("#pagePrevTopButton").disabled = state.page <= 1;
  $("#pageNextButton").disabled = state.page >= pageCount;
  $("#pageNextTopButton").disabled = state.page >= pageCount;
  $("#pageLastButton").disabled = state.page >= pageCount;
  const size = $("#pageSizeSelect");
  if (size && Number(size.value) !== state.limit) size.value = String(state.limit);
}

function goToLibraryPage(page) {
  const pageCount = Math.max(1, Math.ceil(Math.max(0, state.total) / state.limit));
  const next = clamp(Math.round(Number(page || 1)), 1, pageCount);
  if (next === state.page) return;
  state.page = next;
  clearSelection();
  loadVideos();
  $("#videoTableWrap")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

let tableScrollSyncing = false;

function refreshFloatingTableScrollbar() {
  const wrap = $("#videoTableWrap");
  const proxy = $("#tableScrollProxy");
  const track = proxy?.firstElementChild;
  if (!wrap || !proxy || !track) return;
  const drawerOpen = $("#drawer")?.getAttribute("aria-hidden") === "false";
  const overflow = wrap.scrollWidth > wrap.clientWidth + 2;
  const rect = wrap.getBoundingClientRect();
  const inView = rect.bottom > 24 && rect.top < window.innerHeight - 24;
  if (!overflow) {
    if (wrap.scrollLeft !== 0) wrap.scrollLeft = 0;
    if (proxy.scrollLeft !== 0) proxy.scrollLeft = 0;
  }
  proxy.hidden = !(overflow && inView && !drawerOpen);
  if (proxy.hidden) return;
  track.style.width = `${wrap.scrollWidth}px`;
  proxy.style.left = `${Math.max(10, rect.left)}px`;
  proxy.style.width = `${Math.max(120, Math.min(rect.width, window.innerWidth - Math.max(20, rect.left + 10)))}px`;
  if (!tableScrollSyncing) proxy.scrollLeft = wrap.scrollLeft;
}

function initializeFloatingTableScrollbar() {
  const wrap = $("#videoTableWrap");
  const proxy = $("#tableScrollProxy");
  if (!wrap || !proxy || proxy.dataset.bound === "1") return;
  proxy.dataset.bound = "1";
  wrap.addEventListener("scroll", () => {
    if (tableScrollSyncing) return;
    tableScrollSyncing = true;
    proxy.scrollLeft = wrap.scrollLeft;
    tableScrollSyncing = false;
  }, { passive: true });
  proxy.addEventListener("scroll", () => {
    if (tableScrollSyncing) return;
    tableScrollSyncing = true;
    wrap.scrollLeft = proxy.scrollLeft;
    tableScrollSyncing = false;
  }, { passive: true });
  window.addEventListener("resize", refreshFloatingTableScrollbar, { passive: true });
  window.addEventListener("scroll", refreshFloatingTableScrollbar, { passive: true });
  new ResizeObserver(refreshFloatingTableScrollbar).observe(wrap);
  refreshFloatingTableScrollbar();
}

async function loadVideos({ resetPage = false } = {}) {
  if (state.loading) {
    state.reloadRequested = true;
    return;
  }
  if (resetPage) state.page = 1;
  state.loading = true;
  try {
    const requestedOffset = Math.max(0, (state.page - 1) * state.limit);
    const params = new URLSearchParams({
      limit: String(state.limit),
      offset: String(requestedOffset),
      order: state.order,
    });
    if (state.query) params.set("q", state.query);
    if (state.status) params.set("status", state.status);
    if (state.favorite) params.set("favorite", state.favorite);
    if (state.rating) params.set("rating", state.rating);
    if (state.watch) params.set("watch", state.watch);
    if (state.duplicates) params.set("duplicates", state.duplicates);
    const data = await fetchJSON(`/api/videos?${params}`);
    state.total = Number(data.total || 0);
    const pageCount = Math.max(1, Math.ceil(state.total / state.limit));
    if (state.page > pageCount && state.total > 0) {
      state.page = pageCount;
      state.reloadRequested = true;
      return;
    }
    state.videos = data.items || [];
    renderRows();
  } catch (error) {
    toast(`Could not load library: ${error.message}`, "error");
  } finally {
    state.loading = false;
    if (state.reloadRequested) {
      state.reloadRequested = false;
      window.queueMicrotask(() => loadVideos());
    }
  }
}

function metric(label, value) {
  return `<div class="metric-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "—")}</strong></div>`;
}

function detailCard(label, value, small = "") {
  return `<div class="detail-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${small ? `<small>${escapeHtml(small)}</small>` : ""}</div>`;
}

function profileHelp(video) {
  if (video.hsp_diagnostic?.enabled) {
    return `HSP isolation test active: neutralized ${(video.hsp_diagnostic.disabled || []).join(", ")}. Only the HSP served to HereSphere is changed; saved analysis remains intact.`;
  }
  if (video.profile_mode === "manual") {
    return "Manual is explicitly selected for this video. Choose Auto to return to HereTune’s recommendation.";
  }
  if (video.manual_hsp) {
    return "Auto is selected. A headset-written HSP is stored, but it only applies if you explicitly switch this video to Manual.";
  }
  return "Auto is selected. Versioned HSP URLs make HereSphere fetch the newest HereTune profile after recalculation. In HereSphere main settings, enable Always Load HSP for Web API media.";
}

function sceneTimelineHtml(c, durationMs) {
  const scenes = Array.isArray(c?.scene_keyframes) ? c.scene_keyframes : [];
  if (!scenes.length) return "";
  const durationSeconds = Math.max(Number(durationMs || 0) / 1000, Number(scenes.at(-1)?.time_seconds || 0) + 1);
  const markers = scenes.map((scene, index) => {
    const start = Math.max(0, Number(scene.time_seconds || 0));
    const left = clamp(start / Math.max(durationSeconds, 1) * 100, 0, 100);
    const confidence = Math.round(Number(scene.confidence || 0) * 100);
    return `<button type="button" class="scene-marker ${index === 0 ? "active" : ""}" style="--scene-left:${left}%" data-scene-index="${index}" title="Grade ${index + 1} · ${fmtClockSeconds(start)} · ${confidence}% confidence"><span>${index + 1}</span></button>`;
  }).join("");
  const cards = scenes.map((scene, index) => `<article class="scene-grade-card ${index === 0 ? "active" : ""}" data-scene-card="${index}">
    <div><span>GRADE ${index + 1}</span><strong>${fmtClockSeconds(scene.time_seconds || 0)}</strong></div>
    <p>Exp ${signed(scene.exposure_bias || 0, 3)} · WB ${Number(scene.gain_r ?? 1).toFixed(3)}/${Number(scene.gain_g ?? 1).toFixed(3)}/${Number(scene.gain_b ?? 1).toFixed(3)} · Con ${Number(scene.contrast ?? 1).toFixed(3)} · Sat ${Number(scene.saturation ?? 1).toFixed(3)}</p>
    <small>${Math.round(Number(scene.confidence || 0) * 100)}% confidence · ${Number(scene.sample_count || 0)} supporting samples${Number(scene.change_score || 0) ? ` · change ${Number(scene.change_score).toFixed(2)}` : ""}</small>
  </article>`).join("");
  return `<div class="scene-timeline" style="--scene-count:${Math.max(1, scenes.length)}">
    <div class="scene-track"><i></i>${markers}</div>
    <div class="scene-grade-cards">${cards}</div>
  </div>`;
}


function confidenceLabel(value) {
  const percent = Math.round(clamp(Number(value || 0), 0, 1) * 100);
  if (percent >= 82) return ["Strong", "good", percent];
  if (percent >= 62) return ["Good", "medium", percent];
  if (percent >= 40) return ["Limited", "caution", percent];
  return ["Weak", "bad", percent];
}

function confidenceBreakdownHtml(confidence = {}, sceneCount = 0) {
  const rows = [
    ["White balance", "white_balance", "Neutral consistency used by the final WB solve"],
    ["Neutral evidence", "neutral_evidence", "Gray/white pixels suitable for color balance"],
    ["Face / skin", "skin_evidence", "Human-skin evidence used as a guardrail"],
    ["Timeline coverage", "temporal_coverage", "How well accepted samples cover the movie"],
    ["Sample quality", "sample_quality", "Accepted samples after fades/titles/duplicates are removed"],
    ["Tone stability", "tone_stability", "Consistency of exposure and tonal evidence"],
    ["Scene stability", "scene_stability", "Confidence in sustained grading-state boundaries"],
  ];
  return `<div class="confidence-breakdown">${rows.map(([label,key,help]) => {
    if (key === "scene_stability" && Number(sceneCount || 0) <= 1) {
      const singleHelp = "One stable whole-video grade was selected; there is no scene-boundary confidence to score.";
      return `<div class="confidence-breakdown-row" title="${escapeHtml(singleHelp)}"><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(singleHelp)}</small></div><span class="confidence-word neutral">N/A</span><div class="mini-meter na"><i></i></div><b>—</b></div>`;
    }
    const [word, tone, percent] = confidenceLabel(confidence?.[key]);
    return `<div class="confidence-breakdown-row" title="${escapeHtml(help)}"><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(help)}</small></div><span class="confidence-word ${tone}">${word}</span><div class="mini-meter" style="--meter:${percent}%"><i></i></div><b>${percent}%</b></div>`;
  }).join("")}</div>`;
}

function inspectorTimelineHtml(inspector, durationMs) {
  const samples = Array.isArray(inspector?.samples) ? inspector.samples : [];
  const probes = Array.isArray(inspector?.probes) ? inspector.probes : [];
  const scenes = Array.isArray(inspector?.scene_keyframes) ? inspector.scene_keyframes : [];
  const duration = Math.max(Number(inspector?.duration_ms || durationMs || 0) / 1000, 1);
  if (!samples.length && !probes.length) return '<div class="empty-inline">This analysis predates the inspector timeline. Reanalyze once to populate sample-level evidence.</div>';
  const segmentBands = scenes.map((scene, index) => {
    const start = clamp(Number(scene.time_seconds || 0) / duration * 100, 0, 100);
    const endSeconds = index + 1 < scenes.length ? Number(scenes[index + 1].time_seconds || duration) : duration;
    const end = clamp(endSeconds / duration * 100, start, 100);
    return `<i class="inspector-scene-band band-${index % 4}" style="left:${start}%;width:${Math.max(.2,end-start)}%" title="Grade ${index+1}: ${fmtClockSeconds(scene.time_seconds || 0)}–${fmtClockSeconds(endSeconds)}"></i>`;
  }).join("");
  const probeStride = probes.length > 700 ? Math.ceil(probes.length / 700) : 1;
  const probeMarks = probes.filter((_item,index) => index % probeStride === 0).map((item) => {
    const left = clamp(Number(item.time_seconds || 0) / duration * 100, 0, 100);
    return `<i class="inspector-probe ${item.kind === "refine" ? "refine" : ""} ${item.boundary ? "boundary" : ""}" style="left:${left}%" title="${item.kind === "refine" ? "Boundary refinement" : "Scene probe"} · ${fmtClockSeconds(item.time_seconds || 0)}"></i>`;
  }).join("");
  const sampleMarks = samples.map((item, index) => {
    const left = clamp(Number(item.time_seconds || 0) / duration * 100, 0, 100);
    const reason = item.accepted ? "Accepted full sample" : `Rejected: ${item.rejection_reason || "quality"}`;
    return `<button type="button" class="inspector-sample ${item.accepted ? "accepted" : "rejected"}" style="left:${left}%" data-inspector-sample="${index}" title="${escapeHtml(reason)} · ${fmtClockSeconds(item.time_seconds || 0)}"></button>`;
  }).join("");
  return `<div class="analysis-timeline-shell">
    <div class="analysis-timeline-legend"><span><i class="legend-probe"></i>Scene probes</span><span><i class="legend-refine"></i>Boundary refinement</span><span><i class="legend-accepted"></i>Accepted full samples</span><span><i class="legend-rejected"></i>Rejected</span></div>
    <div class="analysis-timeline">${segmentBands}${probeMarks}${sampleMarks}</div>
    <div class="analysis-timeline-axis"><span>0:00</span><span>${fmtClockSeconds(duration/2)}</span><span>${fmtClockSeconds(duration)}</span></div>
    <div class="sample-inspector-detail" id="sampleInspectorDetail"><strong>Click a full-sample marker</strong><span>See the exact frame, source path, luma range, neutral evidence, and rejection decision.</span></div>
  </div>`;
}

function analysisSampleUrl(videoId, time, width = 640) {
  return `/api/videos/${encodeURIComponent(videoId)}/analysis-sample.jpg?kind=full&time_seconds=${encodeURIComponent(time)}&width=${encodeURIComponent(width)}`;
}

function sampleImgTag(video, time, alt, width = 640, className = "", defer = false) {
  const src = analysisSampleUrl(video.id, time, width);
  const fallbackSrc = video?.thumbnail_path
    ? `/thumb/${encodeURIComponent(video.id)}.jpg?rev=${encodeURIComponent(video.thumbnail_revision || "8")}`
    : "/placeholder.svg";
  const fallback = `this.onerror=null;this.src='${fallbackSrc}';this.classList.add('fallback-thumb');`;
  if (defer) {
    return `<img class="${className} live-sample-pending" data-live-sample-src="${src}" alt="${escapeHtml(alt)}" onerror="${fallback}">`;
  }
  return `<img class="${className}" src="${src}" alt="${escapeHtml(alt)}" onerror="${fallback}">`;
}

function initializeLiveSampleImages(root = document) {
  const images = [...root.querySelectorAll('img[data-live-sample-src]')];
  if (!images.length) return;
  const load = (image) => {
    const src = image?.dataset?.liveSampleSrc;
    if (!src) return;
    image.src = src;
    delete image.dataset.liveSampleSrc;
    image.classList.remove('live-sample-pending');
  };
  if (!("IntersectionObserver" in window)) {
    images.forEach(load);
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      load(entry.target);
      observer.unobserve(entry.target);
    });
  }, { root: $("#drawer .drawer-panel") || null, rootMargin: "280px 0px" });
  images.forEach((image) => observer.observe(image));
}

function rejectedGalleryHtml(inspector, video) {
  const samples = (Array.isArray(inspector?.samples) ? inspector.samples : []).filter((item) => !item.accepted);
  if (!samples.length) return '<div class="empty-inline good-empty">No full-resolution samples were rejected in this run.</div>';
  return `<div class="rejected-gallery">${samples.map((item) => {
    const time = Number(item.time_seconds || 0);
    return `<article class="rejected-card">${sampleImgTag(video, time, `Rejected analysis sample at ${fmtClockSeconds(time)}`, 640, 'rejected-card-image', true)}<div><strong>${fmtClockSeconds(time)}</strong><span>${escapeHtml(item.rejection_reason || "Rejected")}</span><small>P50 ${Number(item.p50 || 0).toFixed(3)} · black ${(Number(item.black_fraction || 0)*100).toFixed(1)}% · ${escapeHtml(item.source || "sample")}</small></div></article>`;
  }).join("")}</div>`;
}

function runHistoryHtml(runs = []) {
  if (!Array.isArray(runs) || !runs.length) return '<div class="empty-inline">No run history has been recorded yet.</div>';
  return `<div class="run-history">${runs.map((run) => {
    const when = run.started_at ? new Date(run.started_at).toLocaleString() : "Unknown time";
    const used = Number(run.used_samples || 0);
    const rejected = Number(run.rejected_samples || 0);
    return `<article class="run-history-item ${escapeHtml(run.status || "")}"><span class="run-status-dot"></span><div><strong>${escapeHtml(String(run.status || "run").toUpperCase())} · ${escapeHtml(when)}</strong><small>${escapeHtml(run.analysis_version || "unknown revision")} · ${used} used${rejected ? ` · ${rejected} rejected` : ""} · ${fmtShortSeconds(run.duration_seconds || 0)}</small>${run.error ? `<p>${escapeHtml(run.error)}</p>` : ""}</div></article>`;
  }).join("")}</div>`;
}

function splitRelativePath(value) {
  const raw = String(value || "").replaceAll("\\", "/");
  const parts = raw.split("/").filter(Boolean);
  const filename = parts.pop() || raw || "Unknown file";
  return { filename, folder: parts.length ? parts.join("/") : "Library root" };
}

function duplicatePeersHtml(video) {
  const peers = Array.isArray(video?.duplicates) ? video.duplicates : [];
  if (!video?.duplicate_group || !peers.length) return "";
  const current = splitRelativePath(video.rel_path);
  return `<section class="detail-section duplicate-warning-section" id="drawerDuplicates">
    <div class="detail-section-heading"><div><h3>Likely duplicate media</h3><p>Compare the exact folder and filename pairs HereTune grouped together. Nothing is deleted automatically.</p></div><span class="calibration-badge caution">${peers.length + 1} MATCHES</span></div>
    <div class="duplicate-compare-list">${peers.map(peer => {
      const match = splitRelativePath(peer.rel_path);
      return `<div class="duplicate-compare-row">
        <div class="duplicate-file current"><span>CURRENT FILE</span><strong>${escapeHtml(current.filename)}</strong><small>${escapeHtml(current.folder)}</small></div>
        <div class="duplicate-equals" aria-hidden="true">≈</div>
        <button type="button" class="duplicate-file match" data-open-duplicate="${escapeHtml(peer.id)}"><span>MATCHING FILE</span><strong>${escapeHtml(match.filename)}</strong><small>${escapeHtml(match.folder)}</small></button>
      </div>`;
    }).join("")}</div>
  </section>`;
}


function initializeInspector(inspector, video) {
  if (!inspector) return;
  const samples = Array.isArray(inspector.samples) ? inspector.samples : [];
  $$('[data-inspector-sample]').forEach((marker) => marker.addEventListener('click', () => {
    const item = samples[Number(marker.dataset.inspectorSample)];
    if (!item) return;
    $$('[data-inspector-sample]').forEach((node) => node.classList.remove('selected'));
    marker.classList.add('selected');
    const time = Number(item.time_seconds || 0);
    const detail = $('#sampleInspectorDetail');
    if (detail) detail.innerHTML = `${sampleImgTag(video, time, `Analysis sample at ${fmtClockSeconds(time)}`, 960)}<div><span class="eyebrow">${item.accepted ? "ACCEPTED SAMPLE" : "REJECTED SAMPLE"}</span><strong>${fmtClockSeconds(time)} · ${escapeHtml(item.source || "sample")}</strong><p>${item.accepted ? "Included in the local grade evidence." : `Excluded: ${escapeHtml(item.rejection_reason || "sample-quality rule")}`}</p><small>Luma ${Number(item.p05||0).toFixed(3)} / ${Number(item.p50||0).toFixed(3)} / ${Number(item.p95||0).toFixed(3)} · neutral ${(Number(item.neutral_fraction||0)*100).toFixed(1)}% · black ${(Number(item.black_fraction||0)*100).toFixed(1)}% · face ${Number(item.face_samples||0)} · skin ${Number(item.skin_samples||0)}</small></div>`;
  }));
}

function nativeVectorTable(c) {
  const rows = [
    ["Saturation", "saturation", 1],
    ["Contrast", "contrast", 1],
    ["Gamma", "gamma", 1],
    ["Offset", "offset", 0],
  ];
  return `<div class="channel-vector-table" role="table" aria-label="Per-channel native color controls">
    <div class="channel-vector-row channel-vector-head" role="row"><span>CONTROL</span><span>R</span><span>G</span><span>B</span><span>GLOBAL</span></div>
    ${rows.map(([label, prefix, neutral]) => `<div class="channel-vector-row" role="row">
      <strong>${label}</strong>
      <span>${Number(c?.[`${prefix}_r`] ?? neutral).toFixed(3)}</span>
      <span>${Number(c?.[`${prefix}_g`] ?? neutral).toFixed(3)}</span>
      <span>${Number(c?.[`${prefix}_b`] ?? neutral).toFixed(3)}</span>
      <span>${Number(c?.[prefix] ?? neutral).toFixed(3)}</span>
    </div>`).join("")}
  </div>`;
}

const HSP_DIAGNOSTIC_OPTIONS = [
  ["sharpen", "Sharpen", "Native detail control"],
  ["exposure", "Exposure", "Exposure bias"],
  ["white_temp", "Temperature", "White temperature"],
  ["white_tint", "Tint", "Green / magenta tint"],
  ["gain_rgb", "RGB gain", "Primary residual white balance"],
  ["offset_global", "Global offset", "Additive whole-image offset (unsafe by default)"],
  ["offset_rgb", "RGB offset", "Additive per-channel offsets"],
  ["gamma_global", "Global gamma", "Whole-image gamma"],
  ["gamma_rgb", "RGB gamma", "Per-channel gamma trims"],
  ["contrast_global", "Global contrast", "Whole-image contrast"],
  ["contrast_rgb", "RGB contrast", "Per-channel contrast trims"],
  ["saturation_global", "Global saturation", "Whole-image saturation"],
  ["saturation_rgb", "RGB saturation", "Per-channel saturation trims"],
  ["shadows", "Shadows", "Regional shadow control"],
  ["midtones", "Midtones", "Regional midtone control"],
  ["highlights", "Highlights", "Regional highlight control"],
  ["scene_keyframes", "Scene keyframes", "Use one whole-video grade instead"],
];

const HSP_DIAGNOSTIC_PRESETS = {
  baseline: [],
  noSharpen: ["sharpen"],
  noGlobalOffset: ["offset_global"],
  noRgbOffset: ["offset_rgb"],
  noGlobalGamma: ["gamma_global"],
  noGamma: ["gamma_global", "gamma_rgb"],
  noRegional: ["shadows", "midtones", "highlights"],
  noExposure: ["exposure"],
  noScenes: ["scene_keyframes"],
  neutralImage: HSP_DIAGNOSTIC_OPTIONS.map(([key]) => key),
};

function diagnosticBaselineValue(key, state) {
  const b = state?.baseline_image || {};
  const triple = (prefix, neutral = 1) => ["r", "g", "b"].map((ch) => Number(b?.[`${prefix}_${ch}`] ?? neutral).toFixed(3)).join(" / ");
  const values = {
    sharpen: Number(b.sharpen ?? 2).toFixed(3),
    exposure: `${signed(b.exposure_bias ?? 0, 3)} EV`,
    white_temp: `${Math.round(Number(b.white_temp ?? 6500))} K`,
    white_tint: signed(b.white_tint ?? 0, 4),
    gain_rgb: triple("gain"),
    offset_global: signed(b.offset ?? 0, 3),
    offset_rgb: triple("offset", 0),
    gamma_global: Number(b.gamma ?? 1).toFixed(3),
    gamma_rgb: triple("gamma"),
    contrast_global: Number(b.contrast ?? 1).toFixed(3),
    contrast_rgb: triple("contrast"),
    saturation_global: Number(b.saturation ?? 1).toFixed(3),
    saturation_rgb: triple("saturation"),
    shadows: signed(b.shadows ?? 0, 3),
    midtones: Number(b.midtones ?? 1).toFixed(3),
    highlights: Number(b.highlights ?? 1).toFixed(3),
    scene_keyframes: `${Number(state?.baseline_scene_count || 1)} grade${Number(state?.baseline_scene_count || 1) === 1 ? "" : "s"}`,
  };
  return values[key] || "—";
}

function hspPolicyHtml(video) {
  const policy = video.auto_hsp_policy || {};
  const controls = Array.isArray(policy.controls) ? policy.controls : [];
  const sidecar = video.hsp_sidecar || {};
  const sidecarState = sidecar.current
    ? "The current Auto HSP is exported beside the source video."
    : sidecar.conflict
      ? "A different sidecar already exists. HereTune will not overwrite it without explicit confirmation and a backup."
      : sidecar.exists
        ? "A sidecar exists, but it does not match this video's current Auto HSP."
        : "No sidecar HSP is currently stored beside the source video.";
  const sidecarTone = sidecar.current ? "good" : sidecar.conflict ? "caution" : "medium";
  const overridden = controls.some((item) => item.override !== "inherit");
  const playbackActive = Boolean(video.heresphere_playback_active);
  return `<section class="detail-section hsp-policy-section" id="drawerHspPolicy">
    <div class="detail-section-heading">
      <div><h3>Auto HSP output policy</h3><p>Choose which measured corrections this video may serialize. Inherit follows Settings; Force On/Off affects only this video and rebuilds from saved evidence without re-analysis.</p></div>
      <span class="calibration-badge ${overridden ? "medium" : "good"}">${overridden ? "VIDEO OVERRIDES" : "GLOBAL POLICY"}</span>
    </div>
    <div class="policy-safety-note"><strong>Shadow-floor safety defaults</strong><span>Global Offset and Shadows default to OFF. Global Offset remains outside Auto analysis/tone solving; Force On uses only a separate bounded output suggestion. Every control can still inherit, Force On, or Force Off per video.</span></div>
    <div class="hsp-policy-grid">
      ${controls.map((item) => `<label class="hsp-policy-control ${item.warning ? "caution" : ""}">
        <span><strong>${escapeHtml(item.label)}</strong><small>Global ${item.global ? "ON" : "OFF"} · Effective ${item.effective ? "ON" : "OFF"}${item.warning ? `<br>${escapeHtml(item.warning)}` : ""}</small></span>
        <select data-hsp-policy-control="${escapeHtml(item.name)}" aria-label="${escapeHtml(item.label)} policy">
          <option value="inherit" ${item.override === "inherit" ? "selected" : ""}>Inherit global</option>
          <option value="on" ${item.override === "on" ? "selected" : ""}>Force on</option>
          <option value="off" ${item.override === "off" ? "selected" : ""}>Force off</option>
        </select>
      </label>`).join("")}
    </div>
    <div class="hsp-policy-actions">
      <button class="button primary small" id="applyVideoHspPolicyButton">Save video overrides</button>
      <button class="button ghost small" id="resetVideoHspPolicyButton">Use global policy</button>
      <span>${playbackActive ? "This video is open in HereSphere; changes will wait for its playback-close event." : "Changes rebuild this video's Auto HSP from saved evidence without re-analysis."}</span>
    </div>
    <div class="sidecar-hsp-card">
      <div><span class="eyebrow">PORTABLE HSP SIDECAR</span><strong>${sidecar.global_enabled ? "Automatic Auto-HSP export is enabled" : "Per-video export"}</strong><p>${escapeHtml(sidecarState)}</p><small>${escapeHtml(sidecar.path || "")}</small></div>
      <span class="calibration-badge ${sidecarTone}">${sidecar.current ? "CURRENT" : sidecar.conflict ? "CONFLICT" : sidecar.exists ? "STALE" : "NOT EXPORTED"}</span>
      <div class="sidecar-hsp-actions">
        <button class="button ghost small" id="writeSidecarHspButton" ${playbackActive ? "disabled" : ""}>${sidecar.exists ? "Update Auto HSP beside source file" : "Save Auto HSP beside source file"}</button>
        ${sidecar.exists && sidecar.managed ? `<button class="button danger small" id="removeSidecarHspButton" ${playbackActive ? "disabled" : ""}>Remove HereTune sidecar</button>` : ""}
        ${playbackActive ? '<small>Close the video in HereSphere before writing or removing its sidecar.</small>' : ""}
      </div>
    </div>
  </section>`;
}

function hspDiagnosticLabHtml(video) {
  if (!video.show_hsp_diagnostic_tools) return "";
  const state = video.hsp_diagnostic || {};
  const disabled = new Set(Array.isArray(state.disabled) ? state.disabled : []);
  const activeCount = disabled.size;
  return `<section class="detail-section hsp-diagnostic-lab" id="drawerHspDiagnostic">
    <div class="detail-section-heading">
      <div><h3>HSP isolation lab</h3><p>Temporarily neutralize individual native Image controls for headset A/B testing. Tests change only the HSP served for this video; analysis evidence and the normal Auto profile remain intact.</p></div>
      <span class="calibration-badge ${activeCount ? "caution" : "good"}">${activeCount ? `${activeCount} TEST${activeCount === 1 ? "" : "S"} ACTIVE` : "AUTO BASELINE"}</span>
    </div>
    <div class="diagnostic-note"><span>🥽</span><div><strong>Headset testing</strong><small>Apply a combination, refresh/reopen the HereTune Web API library in HereSphere, then reopen the same movie. Each combination gets a distinct scene identity so cached player state cannot contaminate the comparison.</small></div></div>
    <div class="diagnostic-policy-note"><strong>Global Offset is permanently absent from Auto.</strong><span>Headset testing identified the additive whole-image Offset as the main haze/washed-out culprit. It is locked to native neutral and cannot be enabled by global or per-video policy. RGB Offset remains independent.</span></div>
    <div class="diagnostic-presets">
      <button class="button ghost small" data-hsp-diag-preset="baseline">Current Auto HSP</button>
      <button class="button ghost small" data-hsp-diag-preset="noSharpen">No Sharpen</button>
      <button class="button ghost small" data-hsp-diag-preset="noGlobalOffset">No Global Offset</button>
      <button class="button ghost small" data-hsp-diag-preset="noRgbOffset">No RGB Offset</button>
      <button class="button ghost small" data-hsp-diag-preset="noGlobalGamma">No Global Gamma</button>
      <button class="button ghost small" data-hsp-diag-preset="noRegional">No Shadows/Midtones/Highlights</button>
      <button class="button ghost small" data-hsp-diag-preset="noExposure">No Exposure</button>
      <button class="button ghost small" data-hsp-diag-preset="noScenes">No Scene Grades</button>
      <button class="button danger small" data-hsp-diag-preset="neutralImage">Neutral Image Controls</button>
    </div>
    <div class="diagnostic-grid">
      ${HSP_DIAGNOSTIC_OPTIONS.map(([key, label, note]) => `<label class="diagnostic-toggle">
        <input type="checkbox" data-hsp-diag-control="${key}" ${disabled.has(key) ? "checked" : ""}>
        <span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(note)} · Auto value ${escapeHtml(diagnosticBaselineValue(key, state))}</small></span>
      </label>`).join("")}
    </div>
    <div class="diagnostic-actions">
      <button class="button primary" id="applyHspDiagnosticButton">Apply selected neutralizations</button>
      <button class="button ghost" id="resetHspDiagnosticButton">Restore Auto HSP</button>
      <span>Isolation token <code>${escapeHtml(state.token || "baseline")}</code> · per-video · no re-analysis</span>
    </div>
  </section>`;
}

function unifiedPreviewHtml(video, ready) {
  const revision = encodeURIComponent(video.thumbnail_revision || "8");
  const previewRevision = encodeURIComponent(video.preview_revision || "3");
  const id = encodeURIComponent(video.id);
  const stamp = `${revision}-p${previewRevision}-${Date.now()}`;
  const before = ready
    ? `/preview/${id}/before.jpg?t=${stamp}`
    : `/preview/${id}/final.jpg?t=${stamp}`;
  const after = ready
    ? `/preview/${id}/after.jpg?t=${stamp}`
    : before;
  return `<section class="detail-section visual-workbench" id="drawerPreview">
    <div class="detail-section-heading">
      <div>
        <h3>Before / after preview</h3>
        <p>${ready
          ? "Drag anywhere across the diagonal divider to compare the original sample with the calculated native correction."
          : "A single monitor-friendly sample is available now; the diagonal comparison appears after analysis."}</p>
      </div>
      ${ready
        ? '<span class="calibration-badge good">INTERACTIVE COMPARISON</span>'
        : '<span class="calibration-badge caution">AWAITING ANALYSIS</span>'}
    </div>
    <div class="diagonal-compare ${ready ? "is-compare" : "is-single"}" id="diagonalCompare" style="--split:56%">
      <img class="compare-image compare-before" id="compareBefore" src="${before}" alt="${ready ? "Original sample" : "Video preview"}">
      <div class="compare-after-mask" id="compareAfterMask"><img class="compare-image compare-after" id="compareAfter" src="${after}" alt="Calculated correction"></div>
      <div class="compare-divider" id="compareDivider" aria-hidden="true"><i></i></div>
      <span class="compare-label before">${ready ? "BEFORE" : "PREVIEW"}</span>
      <span class="compare-label after">AFTER</span>
      <input class="compare-range" id="compareRange" type="range" min="0" max="100" value="56" aria-label="Before and after split position" ${ready ? "" : "disabled"}>
    </div>
    <div class="preview-footnote">
      <span>${ready
        ? "Left of the diagonal is the original sample; right is HereTune’s calculated correction."
        : "Analysis has not produced a corrected comparison yet."}</span>
      <small>Exact selected-HSP values · full-strength best-effort monitor approximation · HereSphere remains authoritative.</small>
    </div>
  </section>`;
}

function initializeUnifiedPreview(_video, ready) {
  const viewer = $("#diagonalCompare");
  const range = $("#compareRange");
  if (!viewer || !range || !ready) return;

  const setSplit = (value) => {
    const split = clamp(Number(value), 3, 97);
    viewer.style.setProperty("--split", `${split}%`);
    range.value = String(split);
  };

  range.addEventListener("input", () => setSplit(range.value));
  let dragging = false;
  const splitFromPointer = (event) => {
    const rect = viewer.getBoundingClientRect();
    setSplit((event.clientX - rect.left) / Math.max(rect.width, 1) * 100);
  };
  viewer.addEventListener("pointerdown", (event) => {
    dragging = true;
    viewer.setPointerCapture?.(event.pointerId);
    splitFromPointer(event);
  });
  viewer.addEventListener("pointermove", (event) => {
    if (dragging) splitFromPointer(event);
  });
  viewer.addEventListener("pointerup", (event) => {
    dragging = false;
    viewer.releasePointerCapture?.(event.pointerId);
  });
  viewer.addEventListener("pointercancel", () => { dragging = false; });
  setSplit(range.value);
}

async function openVideo(videoId) {
  state.selectedId = videoId;
  try {
    const [video, inspector] = await Promise.all([
      fetchJSON(`/api/videos/${encodeURIComponent(videoId)}`),
      fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/analysis-inspector`).catch(() => null),
    ]);
    const c = video.correction || {};
    const a = video.analysis || {};
    const layout = video.layout_detection || {};
    const resolvedLayout = layout.resolved || {};
    const alignment = video.stereo_alignment || {};
    const lensVerification = video.lens_verification || {};
    const orientationVerification = video.orientation_verification || {};
    const horizonVerification = video.horizon_verification || {};
    const activeBounds = video.active_image_bounds || {};
    const nativeCalibration = video.native_calibration || {};
    const confidence = Math.round(Number(video.confidence || 0) * 100);
    const ready = video.status === "ready" && Object.keys(c).length > 0;
    const modes = ["original", "mild", "auto", "strong", "manual"];
    const redundantWarningPrefixes = [
      "Lens verification:",
      "VR verification conflict:",
      "Sparse scene grading created",
      "Brightness-dependent color drift was measured",
      "Pixel-based VR layout verification was ambiguous",
    ];
    const warnings = [...new Set(c.warnings || [])]
      .filter((warning) => !redundantWarningPrefixes.some((prefix) => String(warning).startsWith(prefix)))
      .filter((warning) => !(orientationVerification.rotate_180_suspected && String(warning).startsWith("Multiple face detections suggest")))
      .map((warning) => `<div class="warning">${escapeHtml(warning)}</div>`).join("");
    const framesUsed = Number(a.frames_used || 0);
    const framesTarget = Number(a.frames_received || 0) || framesUsed;
    const baseSamples = Number(a.base_samples || 0);
    const samplingPlan = baseSamples && baseSamples !== framesTarget
      ? `${baseSamples} base → ${framesTarget} adaptive target`
      : (framesTarget ? `${framesTarget} target` : "—");
    const tempTintEvidence = Number(a.temperature_tint_samples || 0)
      ? `${Math.round(Number(a.source_white_temp || 6500))}K / ${signed(a.source_white_tint || 0, 3)} · ${Math.round(Number(a.temperature_tint_confidence || 0) * 100)}%`
      : "—";
    const alignmentSummary = Number(alignment.frames || 0)
      ? `${signed(alignment.median_vertical_disparity_px || 0, 1)}px vertical · ${signed(alignment.median_horizontal_disparity_px || 0, 1)}px horizontal · ${signed(alignment.median_roll_deg || 0, 2)}° roll`
      : "No trustworthy stereo matches";
    const scaleSummary = Number(alignment.frames || 0)
      ? `${Number(alignment.median_scale_x || 1).toFixed(4)} × ${Number(alignment.median_scale_y || 1).toFixed(4)} · shear ${signed(alignment.median_shear || 0, 4)}`
      : "Not measured";
    const lensSummary = Number(lensVerification.suggested_fov || 0)
      ? `${Math.round(Number(lensVerification.suggested_fov))}° family hint · ${Math.round(Number(lensVerification.fov_confidence || 0) * 100)}%`
      : "No reliable lens-circle hint";
    const borderSummary = activeBounds.frames
      ? `${activeBounds.significant_borders ? "Persistent border" : "No strong border"} · max ${(Number(activeBounds.largest_border_fraction || 0) * 100).toFixed(1)}%`
      : "Not measured";
    const cropSummary = `${fmtVector(nativeCalibration.zoom, 3, "1.000 / 1.000")} zoom · ${fmtVector(nativeCalibration.pan, 3, "0.000 / 0.000")} pan`;
    const stitchSummary = `shift ${fmtVector(nativeCalibration.stitch_shift, 4)} · scale ${fmtVector(nativeCalibration.stitch_scale, 4, "1.0000 / 1.0000 / 1.0000 / 1.0000")}`;
    const stitchSecondary = `shear ${fmtVector(nativeCalibration.stitch_shear, 4)} · flare ${fmtVector(nativeCalibration.stitch_flare, 4)} · slant ${fmtVector(nativeCalibration.stitch_slant, 4)}`;
    const horizonSummary = Number(horizonVerification.frames || 0)
      ? `${signed(horizonVerification.median_roll_deg || 0, 2)}° measured · ${Math.round(Number(horizonVerification.confidence || 0) * 100)}%`
      : "No stable horizon evidence";
    const autofocusSummary = `${Number(nativeCalibration.autofocus_min_distance || 10).toFixed(1)}–${Number(nativeCalibration.autofocus_max_distance || 1000).toFixed(1)} · speed ${Number(nativeCalibration.autofocus_transition_speed || 1).toFixed(2)}`;
    const audioSummary = `${Number(c.audio_volume_multiplier ?? nativeCalibration.audio_volume_multiplier ?? 1).toFixed(3)}× volume · ${signed(c.audio_time_offset ?? nativeCalibration.audio_time_offset ?? 0, 3)}s offset`;
    const layoutConflictsHtml = (layout.conflicts || []).map((item) => `<div class="calibration-issue">${escapeHtml(item)}</div>`).join("");
    const lensIssuesHtml = (lensVerification.issues || []).map((item) => `<div class="calibration-issue subtle">${escapeHtml(item)}</div>`).join("");
    const sceneCount = Math.max(1, Number(c.scene_count || (Array.isArray(c.scene_keyframes) ? c.scene_keyframes.length : 0) || 1));
    const inspectorSamples = Array.isArray(inspector?.samples) ? inspector.samples : [];
    const inspectorAccepted = inspectorSamples.filter((item) => item.accepted).length;
    const inspectorRejected = inspectorSamples.length - inspectorAccepted;
    const confidenceBreakdown = inspector?.confidence || a.confidence_breakdown || {};
    const runHistory = inspector?.runs || [];

    $("#drawerContent").innerHTML = `
      <div class="drawer-title-block">
        <span class="eyebrow">${escapeHtml(video.status.toUpperCase())} · HSP ${escapeHtml(video.hsp_version ?? "—")} · REV ${escapeHtml(video.hsp_revision || "pending")}</span>
        <h2>${escapeHtml(video.title)}</h2>
        <p class="drawer-path">${escapeHtml(video.rel_path)}</p>
      </div>
      <div class="drawer-overview">
        <div><span>STATUS</span>${statusBadge(video.status)}<small>${video.status === "ready" ? "correction ready" : video.status}</small></div>
        <div><span>WB CONFIDENCE</span><strong>${ready ? `${confidence}%` : "—"}</strong><small>neutral + face/skin</small></div>
        <div><span>SCENE GRADES</span><strong>${ready ? sceneCount : "—"}</strong><small>${sceneCount === 1 ? "stable whole video" : "sustained states"}</small></div>
        <div><span>SAMPLES</span><strong>${inspectorSamples.length ? `${inspectorAccepted}/${inspectorSamples.length}` : (framesUsed || "—")}</strong><small>${inspectorRejected ? `${inspectorRejected} rejected` : "used / examined"}</small></div>
      </div>
      <div class="drawer-control-deck">
        <div class="profile-switch">${modes.map((mode) => {
          const disabled = (mode === "manual" && !video.manual_hsp) || (["mild", "strong"].includes(mode) && !video.auto_hsp_path);
          return `<button data-profile-mode="${mode}" class="${video.profile_mode === mode ? "active" : ""}" ${disabled ? "disabled" : ""}>${mode}</button>`;
        }).join("")}</div>
        <div class="library-editor">
          <button class="favorite-toggle ${video.is_favorite ? "on" : ""}" id="favoriteToggle">${video.is_favorite ? "♥ Favorite" : "♡ Favorite"}</button>
          ${ratingPicker(video.rating)}
          <strong id="ratingValueLabel">${video.rating === null || video.rating === undefined ? "Unrated" : `${Number(video.rating).toFixed(1)} / 5`}</strong>
          ${video.rating === null || video.rating === undefined ? "" : '<button type="button" class="clear-rating" id="clearRatingButton">Clear rating</button>'}
        </div>
      </div>
      <div class="profile-note">${escapeHtml(profileHelp(video))}</div>
      <nav class="drawer-jumpbar" aria-label="Video details"><a href="#drawerPreview">Preview</a><a href="#drawerInspector">Inspector</a><a href="#drawerGrade">Image grade</a>${video.show_hsp_diagnostic_tools ? `<a href="#drawerHspDiagnostic">HSP lab</a>` : ""}<a href="#drawerVr">VR calibration</a><a href="#drawerHistory">History</a></nav>
      ${unifiedPreviewHtml(video, ready)}
      <section class="detail-section inspector-section" id="drawerInspector">
        <div class="detail-section-heading"><div><h3>Analysis inspector</h3><p>Every full-resolution grading sample, the dense scene-probe pass, rejection decisions, and the evidence behind the correction.</p></div><span class="calibration-badge ${inspectorRejected ? "medium" : "good"}">${inspectorSamples.length ? `${inspectorAccepted} USED · ${inspectorRejected} REJECTED` : "NO TIMELINE"}</span></div>
        ${inspectorTimelineHtml(inspector, video.duration_ms)}
        <div class="inspector-columns">
          <div class="inspector-pane"><div class="subsection-heading"><strong>Confidence breakdown</strong><small>What the headline WB confidence is actually made of.</small></div>${confidenceBreakdownHtml(confidenceBreakdown, sceneCount)}</div>
          <div class="inspector-pane"><div class="subsection-heading"><strong>Rejected sample gallery</strong><small>Open the evidence HereTune deliberately excluded.</small></div>${rejectedGalleryHtml(inspector, video)}</div>
        </div>
        <div class="cache-resume-note"><strong>Checkpoint cache</strong><span>${Number(inspector?.cache?.resume_samples || 0)} sample statistics reused · ${Number(inspector?.cache?.saved_samples || 0)} statistics saved this run. Data only—inspector images are decoded live from the original video and are not stored.</span></div>
      </section>
      <section class="detail-section grade-section" id="drawerGrade">
        <div class="detail-section-heading"><div><h3>Native image grade</h3><p>Exact HSP values, including independent RGB nonlinear trims and the global A/W control.</p></div><span class="calibration-badge ${confidence >= 70 ? "good" : confidence >= 40 ? "medium" : "caution"}">${ready ? `${confidence}% evidence` : "pending"}</span></div>
        <div class="detail-grid grade-grid">
          ${detailCard("RGB gain", c.gain_r !== undefined ? `${Number(c.gain_r).toFixed(3)} / ${Number(c.gain_g).toFixed(3)} / ${Number(c.gain_b).toFixed(3)}` : "—", "R / G / B residual")}
          ${detailCard("Temperature", c.white_temp !== undefined ? `${Math.round(Number(c.white_temp))} K` : "—", `source ${Math.round(Number(a.source_white_temp || 6500))} K`)}
          ${detailCard("Tint", c.white_tint !== undefined ? signed(c.white_tint, 4) : "—", "green ↔ magenta")}
          ${detailCard("Exposure", c.exposure_bias !== undefined ? `${signed(c.exposure_bias, 3)} EV` : "—")}
          ${detailCard("Global offset", c.offset !== undefined ? signed(c.offset, 3) : "0.000", "Not generated by Auto · black floor preserved")}
          ${detailCard("Global contrast", c.contrast !== undefined ? Number(c.contrast).toFixed(3) : "—")}
          ${detailCard("Global saturation", c.saturation !== undefined ? Number(c.saturation).toFixed(3) : "—")}
          ${detailCard("Global gamma", c.gamma !== undefined ? Number(c.gamma).toFixed(3) : "—")}
          ${detailCard("Sharpen", c.sharpen !== undefined ? Number(c.sharpen).toFixed(3) : "—", "2.000 native neutral")}
          ${detailCard("Shadows", c.shadows !== undefined ? signed(c.shadows, 3) : "—")}
          ${detailCard("Midtones", c.midtones !== undefined ? Number(c.midtones).toFixed(3) : "—")}
          ${detailCard("Highlights", c.highlights !== undefined ? Number(c.highlights).toFixed(3) : "—")}
        </div>
        <div class="channel-grade-block">
          <div><span class="eyebrow">PER-CHANNEL NONLINEAR COLOR</span><p>Small brightness-dependent trims correct shadow, midtone, highlight, or chroma casts that one RGB gain cannot remove.</p></div>
          <span class="calibration-badge ${Number(c.per_channel_confidence || 0) >= .65 ? "good" : Number(c.per_channel_confidence || 0) >= .35 ? "medium" : "caution"}">${Math.round(Number(c.per_channel_confidence || 0) * 100)}% support</span>
        </div>
        ${nativeVectorTable(c)}
      </section>
      ${hspPolicyHtml(video)}
      ${hspDiagnosticLabHtml(video)}
      <section class="detail-section scene-grade-section">
        <div class="detail-section-heading"><div><h3>Sparse scene-by-scene grade</h3><p>${sceneCount > 1 ? `${sceneCount} stable Image keyframes follow sustained lighting/camera changes.` : "One stable whole-video grade; no sustained change crossed the confidence and hysteresis thresholds."}</p></div><span class="calibration-badge ${sceneCount > 1 ? "medium" : "good"}">${sceneCount} ${sceneCount === 1 ? "GRADE" : "GRADES"}</span></div>
        ${sceneTimelineHtml(c, video.duration_ms) || '<div class="empty-inline">Analysis has not produced a scene timeline yet.</div>'}
        <div class="calibration-policy"><strong>No pumping:</strong> scene grades require minimum duration, sustained before/after evidence, confidence, and hysteresis. Short edits do not create twitchy automatic white-balance changes; the number of grades is evidence-driven.</div>
      </section>
      <section class="detail-section"><h3>Analysis evidence</h3>
        ${metric("Frames analyzed", framesTarget ? `${framesUsed} / ${framesTarget}` : (framesUsed || "—"))}
        ${metric("Sampling plan", samplingPlan)}
        ${metric("Scene probe pass", Number(a.scene_probe_samples || 0) > 0 ? `${a.scene_probe_samples} probes @ ~${Number(a.scene_probe_interval_seconds || 0).toFixed(1)}s / ${a.scene_probe_width || 0}px · ${a.scene_probe_refinement_samples || 0} refinement · ${a.scene_full_samples || framesUsed} full @ ${a.sample_width || 960}px` : "—")}
        ${metric("Frame source", frameSourceText(a))}
        ${metric("Analysis time", Number(a.analysis_seconds || 0) > 0 ? fmtShortSeconds(a.analysis_seconds) : "—")}
        ${metric("Rejected samples", a.rejected_total !== undefined ? `${a.rejected_total} total · ${a.rejected_black_fade || 0} fades/black · ${a.rejected_title_like || 0} title-like · ${a.rejected_duplicate || 0} duplicates` : "—")}
        ${metric("Neutral evidence", a.neutral_fraction !== undefined ? `${(a.neutral_fraction * 100).toFixed(2)}%` : "—")}
        ${metric("Face / skin samples", `${a.face_samples ?? a.face_sample_count ?? 0} / ${a.skin_samples ?? a.skin_sample_count ?? 0}`)}
        ${metric("Median luminance / spread", a.median_luma !== undefined ? `${Number(a.median_luma).toFixed(3)} / ${Number(a.luma_spread || 0).toFixed(3)}` : "—")}
        ${metric("White-balance method", a.white_balance_method || a.wb_method || "—")}
        ${metric("Temperature/tint evidence", tempTintEvidence)}
        ${metric("Per-channel evidence", `${Math.round(Number(c.per_channel_confidence || 0) * 100)}% · ${Number(c.per_channel_samples || 0)} regional samples`)}
        ${metric("Tone confidence", a.tone_confidence !== undefined ? `${Math.round(Number(a.tone_confidence) * 100)}%` : "—")}
        ${metric("Clipping", a.clipped_black_fraction !== undefined ? `${(Number(a.clipped_black_fraction || 0) * 100).toFixed(2)}% black · ${(Number(a.clipped_highlight_fraction || 0) * 100).toFixed(2)}% white` : "—")}
        ${metric("Detail / noise", a.sharpness_score !== undefined ? `${Number(a.sharpness_score || 0).toExponential(2)} / ${Number(a.noise_score || 0).toFixed(4)}` : "—")}
        ${metric("Resolution / size", video.width ? `${video.width} × ${video.height} · ${fmtBytes(video.size)}` : fmtBytes(video.size))}
        ${metric("Stored manual HSP", video.manual_hsp ? `yes · v${video.manual_hsp_version}` : "no")}
      </section>
      <section class="detail-section"><h3>Audio correction & synchronization</h3>
        <div class="calibration-summary-grid compact-cards">
          ${detailCard("Measured audio", Number(c.audio_loudness_windows || 0) ? `${Number(c.audio_mean_db || 0).toFixed(1)} dB mean · ${Number(c.audio_peak_db || 0).toFixed(1)} dB peak` : "No audio evidence", `${Math.round(Number(c.audio_loudness_confidence || 0) * 100)}% · ${Number(c.audio_loudness_windows || 0)} windows`)}
          ${detailCard("Native Audio HSP", audioSummary, nativeCalibration.audio_volume_applied ? "volume applied" : "diagnostic / neutral")}
          ${detailCard("A/V sync suggestion", `${signed(c.suggested_audio_time_offset || 0, 3)}s`, `${Math.round(Number(c.av_sync_confidence || 0) * 100)}% · ${Number(c.av_sync_windows || 0)} windows`)}
          ${detailCard("Drift suggestion", `${signed(c.suggested_sync_drift_seconds_per_hour || 0, 3)}s/hour`, `${Math.round(Number(c.sync_drift_confidence || 0) * 100)}%`)}
        </div>
      </section>
      <section class="detail-section layout-debug-section" id="drawerVr">
        <div class="detail-section-heading"><div><h3>VR verification & native calibration</h3><p>Framing, eye geometry, lens boundaries, horizon, autofocus, and audio controls are independently confidence-gated.</p></div><button class="button small ghost" id="regenerateThumbButton">Regenerate preview</button></div>
        <div class="calibration-summary-grid">
          ${detailCard("Resolved layout", `${resolvedLayout.stereo || video.stereo} / ${resolvedLayout.projection || video.projection}`, `${layout.verification || "cached"} · ${Math.round(Number(layout.confidence || 0) * 100)}%`)}
          ${detailCard("Stereo disparity", alignmentSummary, `${Math.round(Number(alignment.confidence || 0) * 100)}% · ${nativeCalibration.alignment_applied ? "vertical correction applied" : "diagnostic"}`)}
          ${detailCard("Eye scale / shear", scaleSummary, nativeCalibration.stereo_scale_applied ? "native stitch scale applied" : "diagnostic / neutral")}
          ${detailCard("Stitch vectors", stitchSummary, `${stitchSecondary} · ${nativeCalibration.stitch_applied ? "applied" : "diagnostic / neutral"}`)}
          ${detailCard("Encoded bounds", borderSummary, `${cropSummary} · ${nativeCalibration.auto_crop_applied ? "applied" : "diagnostic"}`)}
          ${detailCard("Horizon", horizonSummary, `${fmtVector(nativeCalibration.orientation_rotation, 3)} rotation · ${nativeCalibration.horizon_leveling_applied ? "applied" : "diagnostic"}`)}
          ${detailCard("Lens / FOV", lensSummary, lensVerification.known_profile ? "known profile retained" : nativeCalibration.lens_fov_applied ? "suggested FOV applied" : "verified only")}
          ${detailCard("Autofocus range", autofocusSummary, nativeCalibration.autofocus_applied ? "native range applied" : "recommendation only")}
          ${detailCard("Orientation", orientationVerification.rotate_180_suspected ? "180° rotation suspected" : "No strong 180° evidence", `${orientationVerification.upright_faces || 0} upright / ${orientationVerification.rotated_faces || 0} rotated face hits`)}
        </div>
        ${layoutConflictsHtml || lensIssuesHtml ? `<div class="calibration-issues">${layoutConflictsHtml}${lensIssuesHtml}</div>` : ""}
        <div class="calibration-policy"><strong>Comfort-first defaults:</strong> stable vertical disparity and safe symmetric borders may be corrected automatically. Horizontal disparity remains real depth; roll, scale, stitch, horizon, autofocus, eye order, and generic FOV are opt-in unless manually overridden.</div>
        <details class="layout-override-details">
          <summary><span>Advanced per-video HSP override</span><small>Format, framing, alignment, stitch, focus, audio and sync. Empty fields remain Auto.</small></summary>
          <div class="override-group">
            <div class="override-group-heading"><strong>Format & lens</strong><small>Projection, packing, orientation flags and optical profile.</small></div>
            <div class="layout-controls expanded">
              <label><span>Stereo packing</span><select id="layoutStereo"><option value="auto">Auto</option><option value="sbs">Side-by-side</option><option value="tb">Top/bottom</option><option value="mono">Mono / 2D</option></select></label>
              <label><span>Projection</span><select id="layoutProjection"><option value="auto">Auto</option><option value="perspective">Perspective</option><option value="fisheye">Fisheye</option><option value="equirectangular">Equirectangular 180</option><option value="equirectangular360">Equirectangular 360</option><option value="cubemap">Cubemap</option><option value="equiangularCubemap">Equiangular cubemap</option></select></label>
              <label><span>Eye order</span><select id="layoutEye"><option value="auto">Auto / normal</option><option value="left">Normal (left first)</option><option value="right">Swapped (right first)</option></select></label>
              <label><span>Full-width SBS</span><select id="layoutFullSbs"><option value="auto">Auto</option><option value="true">Yes</option><option value="false">No</option></select></label>
              <label><span>Lens profile</span><input id="layoutLens" type="text" maxlength="100" placeholder="Auto / Linear / MKX200"></label>
              <label><span>FOV</span><input id="layoutFov" type="number" min="30" max="360" step="0.1" placeholder="Auto"></label>
              <label><span>Orientation</span><select id="layoutOrientation"><option value="auto">Auto / up</option><option value="up">Up</option><option value="left">Left</option><option value="right">Right</option><option value="down">Down</option></select></label>
              <label><span>Aspect mode</span><select id="layoutAspectType"><option value="auto">Auto</option><option value="autodetect">Autodetect</option><option value="standard">Standard</option><option value="highdefinition">High definition</option><option value="widescreen">Wide screen</option><option value="custom">Custom</option></select></label>
              <label><span>Custom aspect</span><input id="layoutAspectRatio" type="number" min="0.25" max="8" step="0.01" placeholder="Auto"></label>
              <label><span>Flip front/back</span><select id="layoutFlipFrontBack"><option value="auto">Auto / off</option><option value="true">On</option><option value="false">Off</option></select></label>
              <label><span>Flip left/right</span><select id="layoutFlipLeftRight"><option value="auto">Auto / off</option><option value="true">On</option><option value="false">Off</option></select></label>
              <label><span>Flip up/down</span><select id="layoutFlipUpDown"><option value="auto">Auto / off</option><option value="true">On</option><option value="false">Off</option></select></label>
            </div>
          </div>
          <div class="override-group">
            <div class="override-group-heading"><strong>Framing, alignment & stitch</strong><small>Comma-separated vectors use native HSP component order. Suggestions above are safer starting points than guessing.</small></div>
            <div class="layout-controls expanded">
              <label><span>Zoom X, Y</span><input id="layoutZoom" type="text" inputmode="decimal" placeholder="Auto · 1, 1"></label>
              <label><span>Pan X, Y</span><input id="layoutPan" type="text" inputmode="decimal" placeholder="Auto · 0, 0"></label>
              <label><span>Orientation rotation P, Y, R</span><input id="layoutOrientationRotation" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0"></label>
              <label><span>Alignment position X, Y, Z</span><input id="layoutAlignmentPosition" type="text" inputmode="decimal" placeholder="Auto · 0, 6.5, 0"></label>
              <label><span>Alignment rotation P, Y, R</span><input id="layoutAlignmentRotation" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0"></label>
              <label><span>Stitch shift</span><input id="layoutStitchShift" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0, 0"></label>
              <label><span>Stitch scale</span><input id="layoutStitchScale" type="text" inputmode="decimal" placeholder="Auto · 1, 1, 1, 1"></label>
              <label><span>Stitch shear</span><input id="layoutStitchShear" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0, 0"></label>
              <label><span>Stitch flare</span><input id="layoutStitchFlare" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0, 0"></label>
              <label><span>Stitch slant</span><input id="layoutStitchSlant" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0, 0"></label>
            </div>
          </div>
          <div class="override-group">
            <div class="override-group-heading"><strong>Autofocus, audio & sync</strong><small>Native per-video controls; timing values are seconds and drift is seconds per hour.</small></div>
            <div class="layout-controls expanded">
              <label><span>Focus rotation P, Y, R</span><input id="layoutAutofocusRotation" type="text" inputmode="decimal" placeholder="Auto · 0, 0, 0"></label>
              <label><span>Focal override</span><input id="layoutAutofocusFocalDistance" type="number" min="0" max="5000" step="0.1" placeholder="Auto"></label>
              <label><span>Minimum focus distance</span><input id="layoutAutofocusMinDistance" type="number" min="0" max="5000" step="0.1" placeholder="Auto"></label>
              <label><span>Maximum focus distance</span><input id="layoutAutofocusMaxDistance" type="number" min="0" max="5000" step="0.1" placeholder="Auto"></label>
              <label><span>Focus transition speed</span><input id="layoutAutofocusTransitionSpeed" type="number" min="0" max="20" step="0.05" placeholder="Auto"></label>
              <label><span>Audio time offset</span><input id="layoutAudioTimeOffset" type="number" min="-10" max="10" step="0.001" placeholder="Auto"></label>
              <label><span>Audio volume multiplier</span><input id="layoutAudioVolumeMultiplier" type="number" min="0" max="8" step="0.001" placeholder="Auto"></label>
              <label><span>Sync start offset</span><input id="layoutSyncStartOffset" type="number" min="-10" max="10" step="0.001" placeholder="Auto"></label>
              <label><span>Sync drift / hour</span><input id="layoutSyncDrift" type="number" min="-10" max="10" step="0.001" placeholder="Auto"></label>
            </div>
          </div>
          <div class="layout-actions"><button class="button primary small" id="applyLayoutButton">Apply override & rebuild HSP</button><button class="button ghost small" id="resetLayoutButton">Reset every override to Auto</button></div>
        </details>
      </section>
      ${duplicatePeersHtml(video)}
      <section class="detail-section history-section" id="drawerHistory"><div class="detail-section-heading"><div><h3>Analysis run history</h3><p>Past attempts stay visible so performance, rejection counts, failures, and revision changes are auditable.</p></div><span class="calibration-badge">${runHistory.length} RUN${runHistory.length === 1 ? "" : "S"}</span></div>${runHistoryHtml(runHistory)}</section>
      ${warnings ? `<section class="detail-section"><h3>Conservative warnings</h3>${warnings}</section>` : ""}
      ${video.error ? `<section class="detail-section"><h3>Error</h3><div class="warning">${escapeHtml(video.error)}</div></section>` : ""}
      <div class="drawer-actions">
        ${["pending", "error"].includes(video.status) ? `<button class="button primary" id="priorityButton">${video.manual_priority ? "Remove priority" : "↑ Prioritize analysis"}</button>` : ""}
        <button class="button primary" id="reanalyzeButton">${video.status === "ready" ? "Reanalyze" : "Requeue"}</button>
        ${video.manual_hsp ? `<button class="button danger" id="discardManualButton">Discard stored manual HSP</button>` : ""}
        <a class="button web-player-button" href="/watch/${encodeURIComponent(video.id)}" target="_blank" rel="noopener"><span>▶</span> Watch in browser</a>
      </div>`;

    $$('[data-profile-mode]').forEach((button) => button.addEventListener("click", () => setProfile(video.id, button.dataset.profileMode)));
    $$('[data-hsp-diag-preset]').forEach((button) => button.addEventListener("click", () => {
      const preset = HSP_DIAGNOSTIC_PRESETS[button.dataset.hspDiagPreset] || [];
      setHspDiagnostic(video.id, preset);
    }));
    $("#applyHspDiagnosticButton")?.addEventListener("click", () => {
      const disabled = $$('[data-hsp-diag-control]:checked').map((input) => input.dataset.hspDiagControl);
      setHspDiagnostic(video.id, disabled);
    });
    $("#resetHspDiagnosticButton")?.addEventListener("click", () => setHspDiagnostic(video.id, []));
    $("#applyVideoHspPolicyButton")?.addEventListener("click", () => setVideoHspPolicy(video.id, false));
    $("#resetVideoHspPolicyButton")?.addEventListener("click", () => setVideoHspPolicy(video.id, true));
    $("#writeSidecarHspButton")?.addEventListener("click", () => writeSidecarHsp(video.id));
    $("#removeSidecarHspButton")?.addEventListener("click", () => removeSidecarHsp(video.id));
    $("#favoriteToggle").addEventListener("click", () => updateLibraryState(video.id, { is_favorite: !video.is_favorite }));
    $$('[data-star-rating]').forEach((button) => button.addEventListener("click", () => updateLibraryState(video.id, { rating: Number(button.dataset.starRating) })));
    $("#clearRatingButton")?.addEventListener("click", () => updateLibraryState(video.id, { rating: null }));
    $("#priorityButton")?.addEventListener("click", () => setPriority(video.id, !Boolean(video.manual_priority)));

    const override = video.layout_override || {};
    const boolSelect = (value) => value === true ? "true" : value === false ? "false" : "auto";
    const setValue = (selector, value, fallback = "") => { const node = $(selector); if (node) node.value = value ?? fallback; };
    const vectorValue = (value) => Array.isArray(value) ? value.join(", ") : (value ?? "");
    setValue("#layoutStereo", override.stereo || "auto");
    setValue("#layoutProjection", override.projection || "auto");
    setValue("#layoutEye", override.isEyeSwapped === true ? "right" : override.isEyeSwapped === false ? "left" : "auto");
    setValue("#layoutFullSbs", boolSelect(override.full_side_by_side));
    setValue("#layoutLens", override.lens || "");
    setValue("#layoutFov", override.fov ?? "");
    setValue("#layoutOrientation", override.orientation_type || "auto");
    setValue("#layoutAspectType", override.aspect_ratio_type || "auto");
    setValue("#layoutAspectRatio", override.aspect_ratio ?? "");
    setValue("#layoutZoom", vectorValue(override.zoom));
    setValue("#layoutPan", vectorValue(override.pan));
    setValue("#layoutFlipFrontBack", boolSelect(override.flip_front_back));
    setValue("#layoutFlipLeftRight", boolSelect(override.flip_left_right));
    setValue("#layoutFlipUpDown", boolSelect(override.flip_up_down));
    setValue("#layoutAlignmentPosition", vectorValue(override.alignment_position));
    setValue("#layoutAlignmentRotation", vectorValue(override.alignment_rotation));
    setValue("#layoutOrientationRotation", vectorValue(override.orientation_rotation));
    setValue("#layoutStitchShift", vectorValue(override.stitch_shift));
    setValue("#layoutStitchScale", vectorValue(override.stitch_scale));
    setValue("#layoutStitchShear", vectorValue(override.stitch_shear));
    setValue("#layoutStitchFlare", vectorValue(override.stitch_flare));
    setValue("#layoutStitchSlant", vectorValue(override.stitch_slant));
    setValue("#layoutAutofocusRotation", vectorValue(override.autofocus_rotation));
    setValue("#layoutAutofocusFocalDistance", override.autofocus_focal_distance ?? "");
    setValue("#layoutAutofocusMinDistance", override.autofocus_min_distance ?? "");
    setValue("#layoutAutofocusMaxDistance", override.autofocus_max_distance ?? "");
    setValue("#layoutAutofocusTransitionSpeed", override.autofocus_transition_speed ?? "");
    setValue("#layoutAudioTimeOffset", override.audio_time_offset ?? "");
    setValue("#layoutAudioVolumeMultiplier", override.audio_volume_multiplier ?? "");
    setValue("#layoutSyncStartOffset", override.sync_start_offset_seconds ?? "");
    setValue("#layoutSyncDrift", override.sync_drift_seconds_per_hour ?? "");

    $("#applyLayoutButton")?.addEventListener("click", () => saveLayoutOverride(video.id));
    $("#resetLayoutButton")?.addEventListener("click", () => resetLayoutOverride(video.id));
    $("#regenerateThumbButton")?.addEventListener("click", () => regenerateThumbnail(video.id));
    initializeUnifiedPreview(video, ready);
    initializeInspector(inspector, video);
    initializeRatingPicker(video);
    $$('[data-open-duplicate]').forEach((button) => button.addEventListener('click', () => openVideo(button.dataset.openDuplicate)));
    $$('[data-scene-index]').forEach((marker) => marker.addEventListener("click", () => {
      const index = String(marker.dataset.sceneIndex);
      $$('[data-scene-index]').forEach((item) => item.classList.toggle("active", String(item.dataset.sceneIndex) === index));
      $$('[data-scene-card]').forEach((item) => item.classList.toggle("active", String(item.dataset.sceneCard) === index));
      document.querySelector(`[data-scene-card="${CSS.escape(index)}"]`)?.scrollIntoView?.({ block: "nearest", inline: "nearest", behavior: "smooth" });
    }));
    $("#reanalyzeButton").addEventListener("click", () => queueAnalysis(video.id));
    $("#discardManualButton")?.addEventListener("click", () => discardManual(video.id));
    const drawer = $("#drawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    initializeLiveSampleImages($("#drawerContent"));
    refreshFloatingTableScrollbar();
  } catch (error) {
    toast(`Could not open video: ${error.message}`, "error");
  }
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  state.selectedId = null;
  window.requestAnimationFrame(refreshFloatingTableScrollbar);
}

async function saveLayoutOverride(videoId) {
  try {
    const triState = (selector) => {
      const value = $(selector)?.value || "auto";
      return value === "auto" ? "auto" : value === "true";
    };
    const valueOrAuto = (selector) => {
      const value = $(selector)?.value?.trim?.() || "";
      return value || "auto";
    };
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/layout`, {
      method: "PATCH",
      body: JSON.stringify({
        stereo: $("#layoutStereo").value,
        projection: $("#layoutProjection").value,
        eye: $("#layoutEye").value,
        full_side_by_side: triState("#layoutFullSbs"),
        lens: valueOrAuto("#layoutLens"),
        fov: valueOrAuto("#layoutFov"),
        orientation_type: $("#layoutOrientation").value,
        aspect_ratio_type: $("#layoutAspectType").value,
        aspect_ratio: valueOrAuto("#layoutAspectRatio"),
        zoom: valueOrAuto("#layoutZoom"),
        pan: valueOrAuto("#layoutPan"),
        flip_front_back: triState("#layoutFlipFrontBack"),
        flip_left_right: triState("#layoutFlipLeftRight"),
        flip_up_down: triState("#layoutFlipUpDown"),
        alignment_position: valueOrAuto("#layoutAlignmentPosition"),
        alignment_rotation: valueOrAuto("#layoutAlignmentRotation"),
        orientation_rotation: valueOrAuto("#layoutOrientationRotation"),
        stitch_shift: valueOrAuto("#layoutStitchShift"),
        stitch_scale: valueOrAuto("#layoutStitchScale"),
        stitch_shear: valueOrAuto("#layoutStitchShear"),
        stitch_flare: valueOrAuto("#layoutStitchFlare"),
        stitch_slant: valueOrAuto("#layoutStitchSlant"),
        autofocus_rotation: valueOrAuto("#layoutAutofocusRotation"),
        autofocus_focal_distance: valueOrAuto("#layoutAutofocusFocalDistance"),
        autofocus_min_distance: valueOrAuto("#layoutAutofocusMinDistance"),
        autofocus_max_distance: valueOrAuto("#layoutAutofocusMaxDistance"),
        autofocus_transition_speed: valueOrAuto("#layoutAutofocusTransitionSpeed"),
        audio_time_offset: valueOrAuto("#layoutAudioTimeOffset"),
        audio_volume_multiplier: valueOrAuto("#layoutAudioVolumeMultiplier"),
        sync_start_offset_seconds: valueOrAuto("#layoutSyncStartOffset"),
        sync_drift_seconds_per_hour: valueOrAuto("#layoutSyncDrift"),
      }),
    });
    toast("Native HSP overrides saved; Auto profile and preview rebuilt from existing analysis.", "success");
    await loadVideos();
    await openVideo(videoId);
  } catch (error) {
    toast(`Could not save native override: ${error.message}`, "error");
  }
}

async function resetLayoutOverride(videoId) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/layout`, { method: "PATCH", body: JSON.stringify({ reset: true }) });
    toast("Layout returned to automatic detection.", "success");
    await loadVideos(); await openVideo(videoId);
  } catch (error) { toast(`Could not reset layout: ${error.message}`, "error"); }
}

async function regenerateThumbnail(videoId) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/regenerate-thumbnail`, { method: "POST", body: "{}" });
    toast("Thumbnail regenerated.", "success");
    await loadVideos(); await openVideo(videoId);
  } catch (error) { toast(`Could not regenerate thumbnail: ${error.message}`, "error"); }
}

async function updateLibraryState(videoId, patch) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/library-state`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    toast("Favorite/rating saved. HereSphere will receive the same value on its next request.", "success");
    await loadVideos();
    await openVideo(videoId);
  } catch (error) {
    toast(`Could not save favorite/rating: ${error.message}`, "error");
  }
}

async function setProfile(videoId, mode) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/profile`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    toast(`Profile changed to ${mode}. HereSphere will receive a revisioned HSP URL.`, "success");
    await loadVideos();
    await openVideo(videoId);
  } catch (error) {
    toast(`Profile change failed: ${error.message}`, "error");
  }
}

async function setVideoHspPolicy(videoId, reset = false) {
  try {
    const overrides = {};
    if (!reset) {
      $$('[data-hsp-policy-control]').forEach((select) => {
        const value = String(select.value || "inherit");
        if (value !== "inherit") overrides[String(select.dataset.hspPolicyControl)] = value;
      });
    }
    const result = await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/hsp-policy`, {
      method: "PATCH",
      body: JSON.stringify(reset ? { reset: true } : { overrides }),
    });
    const update = result.policy_update || {};
    toast(
      update.deferred_until_close
        ? "Video policy saved. HereSphere is using this video, so the rebuilt HSP will be applied after it closes."
        : reset
          ? "Video HSP policy reset to the global defaults."
          : "Video HSP policy saved and Auto HSP rebuilt from existing evidence.",
      "success",
    );
    await loadVideos();
    await openVideo(videoId);
  } catch (error) {
    toast(`Could not save video HSP policy: ${error.message}`, "error");
  }
}

async function writeSidecarHsp(videoId, replace = false) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/hsp-sidecar`, {
      method: "POST",
      body: JSON.stringify({ replace }),
    });
    toast("Auto HSP saved safely beside the video.", "success");
    await openVideo(videoId);
  } catch (error) {
    if (!replace && Number(error.status || 0) === 409) {
      const confirmed = window.confirm(
        `${error.message}

Back up the existing sidecar and replace it with this video's current Auto HSP?`
      );
      if (confirmed) return writeSidecarHsp(videoId, true);
    }
    toast(`Could not save sidecar HSP: ${error.message}`, "error");
  }
}

async function removeSidecarHsp(videoId) {
  if (!window.confirm("Remove the HereTune-managed HSP beside this video? The video itself will not be touched.")) return;
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/hsp-sidecar`, {
      method: "DELETE",
    });
    toast("HereTune sidecar removed.", "success");
    await openVideo(videoId);
  } catch (error) {
    toast(`Could not remove sidecar HSP: ${error.message}`, "error");
  }
}

async function setHspDiagnostic(videoId, disabled) {
  try {
    const normalized = [...new Set((disabled || []).filter(Boolean))];
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/hsp-diagnostic`, {
      method: "PATCH",
      body: JSON.stringify({ disabled: normalized }),
    });
    toast(
      normalized.length
        ? "HSP isolation test active. Reopen the movie in HereSphere."
        : "Normal Auto HSP restored. Reopen the movie in HereSphere.",
      "success",
    );
    await loadVideos();
    await openVideo(videoId);
  } catch (error) {
    toast(`Could not apply HSP diagnostic: ${error.message}`, "error");
  }
}

async function queueAnalysis(videoId) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/analyze`, {
      method: "POST",
      body: "{}",
    });
    toast("Analysis queued. Auto will become active when the new profile is ready.", "success");
    closeDrawer();
    await loadVideos();
  } catch (error) {
    toast(`Could not queue analysis: ${error.message}`, "error");
  }
}

async function discardManual(videoId) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/manual-hsp`, { method: "DELETE" });
    toast("Stored manual HSP discarded. Auto remains in charge.", "success");
    await loadVideos();
    await openVideo(videoId);
  } catch (error) {
    toast(`Could not discard manual HSP: ${error.message}`, "error");
  }
}

async function setPriority(videoId, priority) {
  try {
    await fetchJSON(`/api/videos/${encodeURIComponent(videoId)}/priority`, {
      method: "POST",
      body: JSON.stringify({ priority }),
    });
    toast(priority ? "Added to the priority queue. Open Priority queue to drag it into position." : "Returned to normal queue priority.", "success");
    await loadVideos();
    if (state.selectedId === videoId) await openVideo(videoId);
  } catch (error) {
    toast(`Could not change queue priority: ${error.message}`, "error");
  }
}

let draggedQueueId = null;

async function showPriorityQueue() {
  const dialog = $("#queueDialog");
  if (!dialog.open) dialog.showModal();
  try {
    const data = await fetchJSON("/api/queue");
    renderPriorityQueue(data.items || []);
  } catch (error) { toast(`Could not load priority queue: ${error.message}`, "error"); }
}

function renderPriorityQueue(items) {
  const list = $("#priorityQueueList");
  $("#priorityQueueEmpty").hidden = items.length > 0;
  list.innerHTML = items.map((video, index) => `<div class="priority-queue-item" draggable="true" data-queue-id="${escapeHtml(video.id)}"><span class="drag-handle" title="Drag to reorder">⋮⋮</span><span class="queue-rank">${index + 1}</span><img src="${video.thumbnail_path ? `/thumb/${encodeURIComponent(video.id)}.jpg?rev=${encodeURIComponent(video.thumbnail_revision || '7')}` : '/placeholder.svg'}" alt=""><div><strong>${escapeHtml(video.title)}</strong><small>${escapeHtml(video.status)} · ${escapeHtml(video.rel_path)}</small></div><button class="button small ghost" data-remove-priority="${escapeHtml(video.id)}">Remove</button></div>`).join("");
  $$('[data-queue-id]').forEach((item) => {
    item.addEventListener("dragstart", () => { draggedQueueId = item.dataset.queueId; item.classList.add("dragging"); });
    item.addEventListener("dragend", () => { draggedQueueId = null; item.classList.remove("dragging"); });
    item.addEventListener("dragover", (e) => e.preventDefault());
    item.addEventListener("drop", async (e) => {
      e.preventDefault(); const targetId = item.dataset.queueId; if (!draggedQueueId || draggedQueueId === targetId) return;
      const ids = $$('[data-queue-id]').map((node) => node.dataset.queueId);
      const from = ids.indexOf(draggedQueueId), to = ids.indexOf(targetId); ids.splice(to, 0, ids.splice(from, 1)[0]);
      try { const data = await fetchJSON("/api/queue", { method: "PUT", body: JSON.stringify({ ids }) }); renderPriorityQueue(data.items || []); await loadVideos(); }
      catch (error) { toast(`Could not reorder queue: ${error.message}`, "error"); await showPriorityQueue(); }
    });
  });
  $$('[data-remove-priority]').forEach((button) => button.addEventListener("click", async () => { await setPriority(button.dataset.removePriority, false); await showPriorityQueue(); }));
}

async function toggleWorkPause() {
  const paused = Boolean(state.statusPayload?.work?.paused);
  const button = $("#pauseWorkButton");
  button.disabled = true;
  try {
    const result = await fetchJSON("/api/work/pause", {
      method: "POST",
      body: JSON.stringify({ paused: !paused }),
    });
    toast(result.message || (!paused ? "Work paused." : "Work resumed."), "success");
    await refreshStatus();
  } catch (error) {
    toast(`Could not change work state: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function scanLibrary() {
  const button = $("#scanButton");
  button.disabled = true;
  try {
    const result = await fetchJSON("/api/scan", { method: "POST", body: "{}" });
    toast(`Scan complete: ${result.seen || 0} video(s) seen, ${result.added || 0} added, ${result.changed || 0} changed.`, "success");
    await Promise.all([refreshStatus(), loadVideos()]);
  } catch (error) {
    toast(`Scan request failed: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function requeueErrors() {
  try {
    const result = await fetchJSON("/api/analyze/all", {
      method: "POST",
      body: JSON.stringify({ include_ready: false }),
    });
    toast(`${result.queued} errored video(s) queued.`, "success");
    await loadVideos();
  } catch (error) {
    toast(`Queue failed: ${error.message}`, "error");
  }
}

function setDoctorTab(name) {
  const selected = name === "health" ? "health" : "checks";
  $$('[data-doctor-tab]').forEach((button) => {
    const active = button.dataset.doctorTab === selected;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  $$('[data-doctor-panel]').forEach((panel) => { panel.hidden = panel.dataset.doctorPanel !== selected; });
  if (selected === "health") loadLibraryHealth();
}

async function showDoctor(initialTab = "checks") {
  const dialog = $("#doctorDialog");
  if (!dialog.open) dialog.showModal();
  setDoctorTab(initialTab);
  if (initialTab !== "checks" && $("#doctorResults").children.length) return;
  $("#doctorResults").innerHTML = "Running checks…";
  try {
    const data = await fetchJSON("/api/doctor");
    $("#doctorResults").innerHTML = data.checks.map((check) => `<div class="doctor-check"><span class="${check.ok ? "yes" : "no"}">${check.ok ? "✓" : "×"}</span><strong>${escapeHtml(check.name)}</strong><small>${escapeHtml(check.detail || "")}${check.version ? `<br>${escapeHtml(check.version)}` : ""}</small></div>`).join("");
  } catch (error) {
    $("#doctorResults").textContent = error.message;
  }
}


function presetCardsHtml(presets = {}, selected = "") {
  return Object.entries(presets).map(([name, preset]) => {
    const values = preset?.values || {};
    const sampleText = Number(values.max_samples || 0) === 0 ? "unlimited full samples" : `up to ${values.max_samples || "?"} full samples`;
    return `<button type="button" class="preset-card ${name === selected ? "active" : ""}" data-analysis-preset="${escapeHtml(name)}"><strong>${escapeHtml(preset.label || name)}</strong><p>${escapeHtml(preset.description || "")}</p><small>${escapeHtml(`${values.analysis_width || "?"}px · ${values.scene_probe_interval_seconds || "?"}s scene probes · ${sampleText}`)}</small></button>`;
  }).join("");
}

function applyPresetToSettings(name) {
  const presets = state.configPayload?.analysis_presets || state.setupStatus?.presets || {};
  const values = presets?.[name]?.values || {};
  Object.entries(values).forEach(([field, value]) => setSettingValue(`analysis.${field}`, value));
  $$('#settingsPresetCards [data-analysis-preset]').forEach((node) => node.classList.toggle('active', node.dataset.analysisPreset === name));
  toast(`${presets?.[name]?.label || name} preset loaded into the form. Nothing is saved until you click Save settings.`, "success");
}

function bindPresetCards(container, handler) {
  container?.querySelectorAll('[data-analysis-preset]').forEach((button) => button.addEventListener('click', () => handler(button.dataset.analysisPreset)));
}

function renderSettingsPresets(data) {
  const target = $('#settingsPresetCards');
  if (!target) return;
  target.innerHTML = presetCardsHtml(data?.analysis_presets || {}, "");
  bindPresetCards(target, applyPresetToSettings);
}

function normalizedRootValues(values) {
  const seen = new Set();
  const result = [];
  (values || []).forEach((raw) => {
    const value = String(raw || "").trim();
    if (!value) return;
    const key = value.replace(/[\\/]+$/, "").toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    result.push(value);
  });
  return result;
}

function libraryRootRow(value, index, scope) {
  const setup = scope === "setup";
  return `<div class="library-root-row" data-library-root-row="${index}">
    <span class="library-root-icon" aria-hidden="true">${setup ? "＋" : "▣"}</span>
    <label><span>${setup ? `Media folder ${index + 1}` : `Folder ${index + 1}`}</span><input type="text" spellcheck="false" value="${escapeHtml(value)}" data-library-root-input="${index}" placeholder="D:\\VR or \\\\MEDIA-PC\\VR"></label>
    <span class="library-root-state" data-library-root-state="${index}">Not tested</span>
    <button type="button" class="icon-button library-root-remove" data-library-root-remove="${index}" title="Remove folder" aria-label="Remove folder">×</button>
  </div>`;
}

function bindLibraryRootEditor(scope) {
  const setup = scope === "setup";
  const target = setup ? $("#setupLibraryRoots") : $("#settingsLibraryRoots");
  const key = setup ? "setupLibraryRoots" : "settingsLibraryRoots";
  target?.querySelectorAll('[data-library-root-input]').forEach((input) => input.addEventListener('input', () => {
    state[key][Number(input.dataset.libraryRootInput)] = input.value;
  }));
  target?.querySelectorAll('[data-library-root-remove]').forEach((button) => button.addEventListener('click', () => {
    const index = Number(button.dataset.libraryRootRemove);
    state[key].splice(index, 1);
    if (!state[key].length) state[key].push("");
    renderLibraryRootEditor(scope);
  }));
}

function renderLibraryRootEditor(scope) {
  const setup = scope === "setup";
  const target = setup ? $("#setupLibraryRoots") : $("#settingsLibraryRoots");
  const key = setup ? "setupLibraryRoots" : "settingsLibraryRoots";
  if (!target) return;
  if (!state[key].length) state[key] = [""];
  target.innerHTML = state[key].map((value, index) => libraryRootRow(value, index, scope)).join("");
  bindLibraryRootEditor(scope);
  if (!setup) {
    const count = normalizedRootValues(state.settingsLibraryRoots).length;
    $("#libraryRootCount").textContent = `${count} FOLDER${count === 1 ? "" : "S"}`;
  }
}

function addLibraryRoot(scope) {
  const key = scope === "setup" ? "setupLibraryRoots" : "settingsLibraryRoots";
  state[key].push("");
  renderLibraryRootEditor(scope);
  const target = scope === "setup" ? $("#setupLibraryRoots") : $("#settingsLibraryRoots");
  target?.querySelector('[data-library-root-row]:last-child input')?.focus();
}

async function testLibraryRoots(scope) {
  const setup = scope === "setup";
  const key = setup ? "setupLibraryRoots" : "settingsLibraryRoots";
  const resultNode = setup ? $("#setupLibraryResult") : $("#libraryRootsStatus");
  const roots = state[key].map((value) => String(value || "").trim());
  const nonempty = roots.filter(Boolean);
  if (!nonempty.length) {
    resultNode.textContent = "Add at least one media folder first.";
    resultNode.className = setup ? "setup-bad" : "library-roots-bad";
    return false;
  }
  resultNode.textContent = `Testing ${nonempty.length} folder${nonempty.length === 1 ? "" : "s"}…`;
  let okCount = 0;
  for (let index = 0; index < roots.length; index += 1) {
    const root = roots[index];
    const status = (setup ? $("#setupLibraryRoots") : $("#settingsLibraryRoots"))?.querySelector(`[data-library-root-state="${index}"]`);
    if (!root) {
      if (status) { status.textContent = "Blank"; status.className = "library-root-state bad"; }
      continue;
    }
    try {
      const result = await fetchJSON('/api/setup/test-library', { method:'POST', body:JSON.stringify({ root }) });
      if (status) {
        status.textContent = result.ok ? "Reachable" : "Unavailable";
        status.className = `library-root-state ${result.ok ? "good" : "bad"}`;
      }
      if (result.ok) okCount += 1;
    } catch {
      if (status) { status.textContent = "Unavailable"; status.className = "library-root-state bad"; }
    }
  }
  const allOk = okCount === nonempty.length;
  resultNode.textContent = allOk
    ? `All ${okCount} folder${okCount === 1 ? " is" : "s are"} reachable.`
    : `${okCount}/${nonempty.length} folders reachable. Offline network roots can be fixed without removing the others.`;
  resultNode.className = allOk ? (setup ? "setup-ok" : "library-roots-ok") : (setup ? "setup-bad" : "library-roots-bad");
  return allOk;
}

function setupSetStep(index) {
  state.setupStep = clamp(Number(index), 0, 3);
  $$('[data-setup-step]').forEach((node) => node.classList.toggle('active', Number(node.dataset.setupStep) === state.setupStep));
  $$('.setup-progress i').forEach((node, idx) => node.classList.toggle('active', idx <= state.setupStep));
  $('#setupBackButton').disabled = state.setupStep === 0;
  $('#setupNextButton').textContent = state.setupStep === 3 ? 'Finish setup' : 'Next';
  if (state.setupStep === 3) {
    const roots = normalizedRootValues(state.setupLibraryRoots);
    const preset = state.setupStatus?.presets?.[state.setupPreset];
    $('#setupSummary').innerHTML = `<span>Media folders</span><strong>${roots.map(escapeHtml).join('<br>') || 'Not selected'}</strong><span>Analysis profile</span><strong>${escapeHtml(preset?.label || state.setupPreset)}</strong>`;
  }
}

function setupSelectPreset(name) {
  state.setupPreset = name;
  $$('#setupPresetCards [data-analysis-preset]').forEach((node) => node.classList.toggle('active', node.dataset.analysisPreset === name));
}

async function testSetupLibrary() {
  await testLibraryRoots("setup");
}

async function finishSetup() {
  const roots = normalizedRootValues(state.setupLibraryRoots);
  const button = $('#setupNextButton');
  button.disabled = true;
  try {
    const result = await fetchJSON('/api/setup/complete', { method:'POST', body:JSON.stringify({ library_roots:roots, preset:state.setupPreset }) });
    state.configPayload = result.config || state.configPayload;
    $('#setupDialog').close();
    toast(`Setup complete. ${roots.length} media folder${roots.length === 1 ? "" : "s"} joined the library.`, 'success');
    await Promise.all([refreshStatus(), loadVideos()]);
    window.setTimeout(() => startGuidedTour(true), 350);
  } catch (error) {
    toast(`Setup could not finish: ${error.message}`, 'error');
  } finally {
    button.disabled = false;
  }
}

async function setupNext() {
  if (state.setupStep === 0) {
    const roots = normalizedRootValues(state.setupLibraryRoots);
    if (!roots.length) { toast('Add at least one media-library folder first.', 'error'); return; }
  }
  if (state.setupStep === 3) { await finishSetup(); return; }
  setupSetStep(state.setupStep + 1);
}

async function initializeFirstRun() {
  try {
    const data = await fetchJSON('/api/setup/status');
    state.setupStatus = data;
    if (!data.required) return;
    state.setupLibraryRoots = Array.isArray(data.library_roots) && data.library_roots.length ? [...data.library_roots] : [""];
    renderLibraryRootEditor("setup");
    $('#setupPresetCards').innerHTML = presetCardsHtml(data.presets || {}, state.setupPreset);
    bindPresetCards($('#setupPresetCards'), setupSelectPreset);
    $('#setupFfmpegMark').textContent = data.ffmpeg_ok ? '✓' : '×';
    $('#setupFfmpegText').textContent = data.ffmpeg_ok ? 'Found and ready.' : 'Not found. Install FFmpeg or set its path later in Settings.';
    $('#setupFfprobeMark').textContent = data.ffprobe_ok ? '✓' : '×';
    $('#setupFfprobeText').textContent = data.ffprobe_ok ? 'Found and ready.' : 'Not found. FFprobe ships with normal FFmpeg builds.';
    const accel = data.acceleration || {};
    const gpuActive = Boolean(accel.cuda_usable) && ['gpu', 'hybrid'].includes(String(accel.resolved_mode || '').toLowerCase());
    $('#setupAccelMark').textContent = gpuActive ? '✓' : '○';
    $('#setupAccelText').textContent = gpuActive
      ? `${String(accel.resolved_mode || 'GPU').toUpperCase()} is available; HereTune can use GPU-assisted sampling.`
      : 'CPU mode is available. NVIDIA/CUDA acceleration is optional, not a hostage situation.';
    setupSetStep(0);
    $('#setupDialog').showModal();
  } catch (error) {
    console.warn('[HereTune] first-run setup check failed', error);
  }
}

const TOUR_STEPS = [
  { target:'.hero', title:'Your HereSphere companion', copy:'Add the displayed URL as a HereSphere Web API library and enable Always Load HSP in HereSphere. The original media stays original; HereTune just supplies better instructions.' },
  { target:'.stats', title:'Library at a glance', copy:'These cards show indexed media, completed corrections, queue size/ETA, and average white-balance evidence. If the numbers look haunted, Doctor is where we interrogate the ghosts.' },
  { target:'.library-panel', title:'Correction queue', copy:'Search, filter, prioritize, inspect duplicates, and open any Ready video for the full analysis inspector. Priority requests are FIFO unless you deliberately reorder them.' },
  { target:'#settingsButton', title:'Settings without TOML archaeology', copy:'Every serious analysis knob remains available here. Presets are just starting points: apply one, change whatever you want, then save.' },
  { target:'#doctorButton', title:'Doctor & Library Health', copy:'Runtime checks, GPU/FFmpeg diagnostics, database health, cache/storage information, and safe maintenance live together here. Tiny stethoscope not included.' },
];

function clearTourHighlight() {
  document.querySelector('.tour-highlight')?.classList.remove('tour-highlight');
}

function renderTourStep() {
  clearTourHighlight();
  const step = TOUR_STEPS[state.tourIndex];
  if (!step) { finishTour(); return; }
  const target = document.querySelector(step.target);
  if (target && !target.hidden) {
    target.classList.add('tour-highlight');
    target.scrollIntoView({ behavior:'smooth', block:'center' });
  }
  $('#tourTitle').textContent = step.title;
  $('#tourCopy').textContent = step.copy;
  $('#tourCount').textContent = `${state.tourIndex + 1} of ${TOUR_STEPS.length}`;
  $('#tourNextButton').textContent = state.tourIndex === TOUR_STEPS.length - 1 ? 'Done' : 'Next';
}

function startGuidedTour(force = false) {
  if (!force && window.localStorage.getItem('heretune-guided-tour-v1') === 'done') return;
  state.tourIndex = 0;
  document.body.classList.add('tour-active');
  const dialog = $('#tourDialog');
  if (!dialog.open) dialog.show();
  renderTourStep();
}

function finishTour() {
  clearTourHighlight();
  document.body.classList.remove('tour-active');
  $('#tourDialog')?.close();
  window.localStorage.setItem('heretune-guided-tour-v1', 'done');
}

function settingElement(name) {
  return document.querySelector(`[name="${CSS.escape(name)}"]`);
}

function setSettingValue(name, value) {
  const element = settingElement(name);
  if (!element) return;
  if (element.type === "checkbox") {
    element.checked = Boolean(value);
  } else {
    element.value = value ?? "";
  }
}

function fillSettings(data) {
  state.configPayload = data;
  renderSettingsPresets(data);
  state.settingsLibraryRoots = Array.isArray(data?.library?.roots) ? [...data.library.roots] : [];
  if (!state.settingsLibraryRoots.length) state.settingsLibraryRoots = [""];
  renderLibraryRootEditor("settings");
  for (const section of ["library", "server", "analysis", "hsp", "paths"]) {
    const values = data[section] || {};
    for (const [name, value] of Object.entries(values)) {
      if (section === "server" && name === "dashboard_password") continue;
      setSettingValue(`${section}.${name}`, value);
    }
  }
  $("#settingsPath").textContent = data.config_path || "config.toml";
  const overrides = Object.entries(data.environment_overrides || {}).filter(([, enabled]) => enabled).map(([name]) => name);
  $("#settingsHint").textContent = overrides.length
    ? `Environment override active for: ${overrides.join(", ")}. It remains authoritative for that setting.`
    : "Changes take effect only after Save. Most settings hot-apply; bind host, port, and data directory require restart.";
}

async function showSettings() {
  const dialog = $("#settingsDialog");
  dialog.showModal();
  $("#saveSettingsButton").disabled = true;
  try {
    const data = await fetchJSON("/api/config");
    fillSettings(data);
  } catch (error) {
    toast(`Could not load settings: ${error.message}`, "error");
  } finally {
    $("#saveSettingsButton").disabled = false;
  }
}

function formValue(element) {
  if (element.type === "checkbox") return element.checked;
  if (element.type === "number") return element.value === "" ? 0 : Number(element.value);
  return element.value;
}

function settingsPayload() {
  const payload = { library: { roots: normalizedRootValues(state.settingsLibraryRoots) }, server: {}, analysis: {}, hsp: {}, paths: {} };
  $$("#settingsForm [name]").forEach((element) => {
    const [section, name] = element.name.split(".", 2);
    if (!payload[section] || !name) return;
    const value = formValue(element);
    if (section === "server" && name === "dashboard_password" && !String(value || "")) return;
    payload[section][name] = value;
  });
  return payload;
}

async function saveSettings() {
  const button = $("#saveSettingsButton");
  button.disabled = true;
  try {
    const roots = normalizedRootValues(state.settingsLibraryRoots);
    if (!roots.length) throw new Error("Add at least one media folder before saving.");
    const result = await fetchJSON("/api/config", {
      method: "PUT",
      body: JSON.stringify(settingsPayload()),
    });
    if (result.config) fillSettings(result.config);
    const restartFields = Array.isArray(result.restart_required_for) ? result.restart_required_for : [];
    const restartLabels = {
      "server.host": "bind host",
      "server.port": "server port",
      "paths.data_dir": "data directory",
    };
    const restartText = restartFields.map((key) => restartLabels[key] || key).join(", ");
    toast(result.message || (restartFields.length ? `Settings saved and applied. Restart required for: ${restartText}.` : "Settings saved and applied."), "success");
    const notes = [];
    if (result.sidecar_sync_scheduled) notes.push("Existing Ready videos are synchronizing portable HSP sidecars in the background; progress appears in the work strip.");
    if (result.analysis_changes_apply_next_job) notes.push("New analysis settings start with the next video/requeue; an active video keeps the settings it started with.");
    if (result.startup_preference_applies_next_launch) notes.push("The Start paused preference applies on the next launch; the current Pause/Resume state is unchanged.");
    if (restartFields.length) notes.push(`Restart required for ${restartText}.`);
    $("#settingsHint").textContent = notes.length ? notes.join(" ") : "Saved and applied to the running HereTune server.";
    await refreshStatus();
    if (state.selectedId && $("#drawer")?.classList.contains("open")) await openVideo(state.selectedId);
  } catch (error) {
    toast(`Settings were not saved: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}


function healthStat(label, value, note = "", tone = "") {
  return `<article class="health-stat ${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ""}</article>`;
}

async function loadLibraryHealth() {
  const target = $('#healthResults');
  if (!target) return;
  target.innerHTML = '<div class="empty-inline">Checking database, cache, thumbnails, duplicates, and library paths…</div>';
  try {
    const data = await fetchJSON('/api/library/health');
    const storage = data.storage || {};
    const disk = storage.disk || {};
    const dbSize = Object.values(storage.database || {}).reduce((sum, value) => sum + Number(value || 0), 0);
    const integrity = Array.isArray(data.integrity) ? data.integrity : [];
    const integrityOk = integrity.length === 1 && String(integrity[0]).toLowerCase() === 'ok';
    const rootStates = Array.isArray(data.library_roots) ? data.library_roots : [];
    const rootSummary = rootStates.length
      ? rootStates.map((item) => `<span class="health-root ${item.reachable ? "good" : "bad"}"><i>${item.reachable ? "✓" : "×"}</i>${escapeHtml(item.root)}</span>`).join("")
      : '<span class="health-root bad"><i>×</i>No media folders configured</span>';
    target.innerHTML = `<div class="health-hero ${data.root_reachable && integrityOk ? "good" : "caution"}"><div><span class="eyebrow">${data.roots_reachable || 0}/${data.roots_configured || 0} MEDIA FOLDERS ONLINE</span><strong>${data.root_reachable ? "Library roots reachable" : "One or more library roots need attention"}</strong><div class="health-root-list">${rootSummary}</div><small>Database integrity: ${integrityOk ? "OK" : escapeHtml(integrity.join(', ') || 'unknown')}</small></div><div class="health-orb"></div></div>
      <div class="health-stat-grid">
        ${healthStat('Ready / analyzed', fmtNumber(data.statuses?.ready || 0), `${fmtNumber(data.analysis?.stale_ready || 0)} stale revision`, data.analysis?.stale_ready ? 'caution' : 'good')}
        ${healthStat('Errors', fmtNumber(data.profiles?.errors || 0), 'persisted analysis failures', data.profiles?.errors ? 'caution' : '')}
        ${healthStat('Missing on disk', fmtNumber(data.missing_on_disk || 0), `${fmtNumber(data.inaccessible || 0)} inaccessible`, data.missing_on_disk || data.inaccessible ? 'bad' : 'good')}
        ${healthStat('Duplicate groups', fmtNumber(data.duplicates?.groups || 0), `${fmtNumber(data.duplicates?.members || 0)} matching files`)}
        ${healthStat('Checkpoint cache', fmtBytes(storage.analysis_cache_bytes || 0), `statistics only · limit ${fmtBytes(storage.analysis_cache_limit_bytes || 0)}`)}
        ${healthStat('Database', fmtBytes(dbSize), `WAL ${fmtBytes(storage.database?.wal || 0)}`)}
        ${healthStat('Thumbnails', fmtBytes(storage.thumbnail_bytes || 0), `${fmtNumber(storage.orphan_thumbnails || 0)} orphaned`, storage.orphan_thumbnails ? 'caution' : '')}
        ${healthStat('Free data disk', fmtBytes(disk.free || 0), `${disk.total ? Math.round(Number(disk.free||0)/Number(disk.total)*100) : 0}% free`, disk.total && Number(disk.free||0)/Number(disk.total) < .08 ? 'bad' : '')}
      </div>
      <section class="health-maintenance"><div class="subsection-heading"><strong>Safe maintenance</strong><small>These operations refuse to start while analysis is active.</small></div><div class="health-actions"><button class="button ghost small" data-maintenance="backup">Back up database</button><button class="button ghost small" data-maintenance="integrity">Run integrity check</button><button class="button ghost small" data-maintenance="prune-cache">Prune analysis cache</button><button class="button ghost small" data-maintenance="clean-thumbnails">Clean orphan thumbnails</button><button class="button ghost small" data-maintenance="vacuum">Vacuum database</button></div></section>
      <section class="health-backups"><div class="subsection-heading"><strong>Recent backups</strong><small>Maintenance backups protect the analysis database.</small></div>${Array.isArray(data.backups) && data.backups.length ? `<div class="backup-list">${data.backups.map(item => `<div><strong>${escapeHtml(item.name)}</strong><span>${fmtBytes(item.bytes)} · ${item.modified ? new Date(item.modified).toLocaleString() : ''}</span></div>`).join('')}</div>` : '<div class="empty-inline">No backups yet.</div>'}</section>`;
    $$('[data-maintenance]').forEach(button => button.addEventListener('click', () => runMaintenance(button.dataset.maintenance, button)));
  } catch (error) {
    target.innerHTML = `<div class="warning">Could not load library health: ${escapeHtml(error.message)}</div>`;
  }
}

async function runMaintenance(action, button) {
  if (!action) return;
  const original = button?.textContent || 'Working…';
  if (button) { button.disabled = true; button.textContent = 'Working…'; }
  try {
    const result = await fetchJSON('/api/library/maintenance', { method: 'POST', body: JSON.stringify({ action }) });
    toast(`${action.replaceAll('-', ' ')} complete.`, 'success');
    await loadLibraryHealth();
  } catch (error) {
    toast(`Maintenance could not run: ${error.message}`, 'error');
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

async function showLibraryHealth() {
  await showDoctor("health");
}

// Direct native-media handoff UI is intentionally not exposed.

let liveRefreshTimer = 0;
let liveLibraryTimer = 0;
function scheduleStatusRefresh(delay = 80) { clearTimeout(liveRefreshTimer); liveRefreshTimer = setTimeout(refreshStatus, delay); }
function scheduleLibraryRefresh(delay = 120) { clearTimeout(liveLibraryTimer); liveLibraryTimer = setTimeout(() => loadVideos(), delay); }
function connectLiveEvents() {
  if (!("EventSource" in window)) return;
  try {
    const source = new EventSource("/api/live");
    state.liveSource = source;
    source.addEventListener("hello", () => { state.liveConnected = true; $("#healthPulse")?.classList.add("live"); });
    source.addEventListener("analysis", (event) => {
      try { const data = JSON.parse(event.data || "{}"); if (data.stage === "ready") scheduleLibraryRefresh(20); } catch {}
      scheduleStatusRefresh(20);
    });
    ["library", "queue"].forEach((name) => source.addEventListener(name, () => { scheduleStatusRefresh(30); scheduleLibraryRefresh(60); }));
    source.addEventListener("sidecar-sync", (event) => {
      scheduleStatusRefresh(120);
      try {
        const data = JSON.parse(event.data || "{}");
        if (data.state === "complete") {
          const key = String(data.completed_at || `${data.written}|${data.unchanged}|${data.conflicts}|${data.errors}`);
          if (key !== state.lastSidecarSyncToast) {
            state.lastSidecarSyncToast = key;
            const bits = [
              `${fmtNumber(data.written || 0)} created/updated`,
              `${fmtNumber(data.unchanged || 0)} already current`,
              `${fmtNumber(data.conflicts || 0)} conflicts`,
              `${fmtNumber(data.deferred || 0)} deferred`,
              `${fmtNumber(data.errors || 0)} errors`,
            ];
            toast(`HSP sidecar sync complete · ${bits.join(" · ")}`, Number(data.errors || 0) ? "error" : "success");
          }
        } else if (data.state === "error") {
          toast(`HSP sidecar sync failed: ${data.error || "unknown error"}`, "error");
        }
      } catch {}
    });
    ["status", "scan", "resources"].forEach((name) => source.addEventListener(name, () => scheduleStatusRefresh(40)));
    source.onerror = () => { state.liveConnected = false; $("#healthPulse")?.classList.remove("live"); };
  } catch {}
}

function updateResetFiltersState() {
  const active = Boolean(
    state.query || state.status || state.favorite !== "all" || state.rating !== "all" ||
    state.watch !== "all" || state.duplicates !== "all" || state.order !== "title"
  );
  $("#resetFiltersButton")?.classList.toggle("active", active);
}

function resetLibraryFilters() {
  state.query = "";
  state.status = "";
  state.favorite = "all";
  state.rating = "all";
  state.watch = "all";
  state.duplicates = "all";
  state.order = "title";
  $("#searchInput").value = "";
  $("#statusFilter").value = "";
  $("#favoriteFilter").value = "all";
  $("#ratingFilter").value = "all";
  $("#watchFilter").value = "all";
  $("#duplicateFilter").value = "all";
  $("#orderSelect").value = "title";
  updateResetFiltersState();
  state.page = 1;
  loadVideos();
}

let searchTimer;
$("#searchInput").addEventListener("input", (event) => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    state.query = event.target.value.trim();
    clearSelection();
    updateResetFiltersState();
    loadVideos({ resetPage: true });
  }, 250);
});
$("#statusFilter").addEventListener("change", (event) => {
  state.status = event.target.value;
  clearSelection();
  updateResetFiltersState();
  loadVideos({ resetPage: true });
});
$("#favoriteFilter").addEventListener("change", (event) => {
  state.favorite = event.target.value;
  clearSelection();
  updateResetFiltersState();
  loadVideos({ resetPage: true });
});
$("#ratingFilter").addEventListener("change", (event) => {
  state.rating = event.target.value;
  clearSelection();
  updateResetFiltersState();
  loadVideos({ resetPage: true });
});
$("#watchFilter").addEventListener("change", (event) => { state.watch = event.target.value; clearSelection(); updateResetFiltersState(); loadVideos({ resetPage: true }); });
$("#duplicateFilter").addEventListener("change", (event) => { state.duplicates = event.target.value; clearSelection(); updateResetFiltersState(); loadVideos({ resetPage: true }); });
$("#orderSelect").addEventListener("change", (event) => {
  state.order = event.target.value;
  updateResetFiltersState();
  loadVideos({ resetPage: true });
});
$("#pageFirstButton").addEventListener("click", () => goToLibraryPage(1));
$("#pagePrevButton").addEventListener("click", () => goToLibraryPage(state.page - 1));
$("#pagePrevTopButton").addEventListener("click", () => goToLibraryPage(state.page - 1));
$("#pageNextButton").addEventListener("click", () => goToLibraryPage(state.page + 1));
$("#pageNextTopButton").addEventListener("click", () => goToLibraryPage(state.page + 1));
$("#pageLastButton").addEventListener("click", () => goToLibraryPage(Math.ceil(Math.max(1, state.total) / state.limit)));
$("#pageSizeSelect").addEventListener("change", (event) => {
  state.limit = Number(event.target.value || 50);
  state.page = 1;
  clearSelection();
  loadVideos();
});
$("#scanButton").addEventListener("click", scanLibrary);
$("#pauseWorkButton").addEventListener("click", toggleWorkPause);
$("#analyzeAllButton").addEventListener("click", requeueErrors);
$("#selectAllButton").addEventListener("click", selectAllMatching);
$("#selectVisibleCheckbox").addEventListener("change", (event) => {
  state.selectionAllMatching = false;
  state.videos.forEach((video) => {
    const id = String(video.id);
    if (event.target.checked) state.selectedIds.add(id); else state.selectedIds.delete(id);
  });
  updateSelectionUi();
});
$("#prioritizeSelectionButton").addEventListener("click", prioritizeSelection);
$("#clearSelectionButton").addEventListener("click", clearSelection);
$("#resetFiltersButton").addEventListener("click", () => { clearSelection(); resetLibraryFilters(); });
$("#doctorButton").addEventListener("click", () => showDoctor("checks"));
$$('[data-doctor-tab]').forEach((button) => button.addEventListener('click', () => setDoctorTab(button.dataset.doctorTab)));
$("#queueButton").addEventListener("click", showPriorityQueue);
$("#refreshHealthButton").addEventListener("click", loadLibraryHealth);
$("#settingsButton").addEventListener("click", showSettings);
$("#footerSettingsButton")?.addEventListener("click", showSettings);
$("#footerDoctorButton")?.addEventListener("click", () => showDoctor("checks"));
$("#saveSettingsButton").addEventListener("click", saveSettings);
$("#addLibraryRootButton").addEventListener("click", () => addLibraryRoot("settings"));
$("#testLibraryRootsButton").addEventListener("click", () => testLibraryRoots("settings"));
$("#setupAddLibraryRoot").addEventListener("click", () => addLibraryRoot("setup"));
$("#setupTestLibrary").addEventListener("click", testSetupLibrary);
$("#setupBackButton").addEventListener("click", () => setupSetStep(state.setupStep - 1));
$("#setupNextButton").addEventListener("click", setupNext);
$("#takeTourButton").addEventListener("click", () => { $("#settingsDialog").close(); startGuidedTour(true); });
$("#tourNextButton").addEventListener("click", () => { if (state.tourIndex >= TOUR_STEPS.length - 1) finishTour(); else { state.tourIndex += 1; renderTourStep(); } });
$("#tourSkipButton").addEventListener("click", finishTour);
$("#copyUrl").addEventListener("click", async () => {
  const button = $("#copyUrl");
  const originalText = button.textContent;
  const ok = await copyText(endpointUrl());
  if (ok) {
    button.textContent = "Copied!";
    button.classList.add("copied");
    toast("HereSphere URL copied.", "success");
    window.setTimeout(() => {
      button.textContent = originalText;
      button.classList.remove("copied");
    }, 1400);
  } else {
    const target = $("#heresphereUrl");
    const range = document.createRange();
    range.selectNodeContents(target);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast("Automatic copy was blocked. The URL is selected so you can press Ctrl+C.", "error");
  }
});
$$('[data-close-drawer]').forEach((node) => node.addEventListener("click", closeDrawer));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawer();
});

updateResetFiltersState();
initializeFloatingTableScrollbar();
initializeFirstRun();
refreshStatus();
loadVideos();
connectLiveEvents();
// SSE is the primary update path. These slow timers are deliberately retained as
// a fallback for browsers/proxies that disable EventSource.
window.setInterval(refreshStatus, 20000);
window.setInterval(() => loadVideos(), 60000);
