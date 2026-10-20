/**
 * Background voice waves for the Talk view: a few sine lines whose amplitude
 * follows the user's microphone level (conversation.level, role "user").
 * Frozen, they lie flat and still — the privacy hat is on, nothing is heard.
 */

import { h } from "./ui.js";

// conversation.level arrives at ~15 Hz; past this gap the mic is muted or
// silent, so the level falls back to zero instead of holding its last value.
const LEVEL_STALE_MS = 250;
const SMOOTHING = 0.18;
const RESTING_AMPLITUDE = 0.04; // a faint ripple while ready, before anyone speaks
const WAVES = Object.freeze([
  { cycles: 1.6, speed: 1.1, offset: 0, weight: 1, alpha: 0.9 },
  { cycles: 2.3, speed: -1.6, offset: 1.9, weight: 0.7, alpha: 0.55 },
  { cycles: 3.1, speed: 2.2, offset: 3.7, weight: 0.45, alpha: 0.35 },
]);

/** Build the waves canvas, centred vertically on ``anchor``. Returns { root, setLevel, setFrozen, dispose }. */
export function createVoiceWaves({ anchor } = {}) {
  const root = h("canvas", { class: "voice-waves", "aria-hidden": "true" });
  const ctx = root.getContext("2d");
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");

  let target = 0;
  let level = 0;
  let lastLevelAt = 0;
  let frozen = false;
  let phase = 0;
  let lastFrame = performance.now();
  let frame = null;
  let disposed = false;
  let width = 0;
  let height = 0;
  let color = "#22d3ee";

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    width = window.innerWidth;
    height = window.innerHeight;
    root.width = Math.round(width * dpr);
    root.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    color = getComputedStyle(root).getPropertyValue("--wave-color").trim() || color;
  }

  function draw(now) {
    const dt = Math.min(0.1, (now - lastFrame) / 1000);
    lastFrame = now;
    if (now - lastLevelAt > LEVEL_STALE_MS) target = 0;
    const goal = frozen ? 0 : Math.max(RESTING_AMPLITUDE, target);
    level += (goal - level) * SMOOTHING;
    if (!frozen && !reducedMotion?.matches) phase += dt * (1 + level * 4);

    // Followed every frame: the orb shifts as captions and badges come and go.
    const rect = anchor?.getBoundingClientRect();
    const midline = rect && rect.height > 0 ? rect.top + rect.height / 2 : height / 2;
    ctx.clearRect(0, 0, width, height);
    const amplitude = level * Math.min(height * 0.22, 160);
    ctx.lineWidth = 2;
    ctx.strokeStyle = color;
    for (const wave of WAVES) {
      ctx.globalAlpha = wave.alpha * (frozen ? 0.35 : 1);
      ctx.beginPath();
      for (let x = 0; x <= width; x += 4) {
        const t = x / width;
        const envelope = Math.sin(Math.PI * t) ** 2; // pinned at both edges
        const y =
          midline +
          amplitude * wave.weight * envelope *
            Math.sin(t * wave.cycles * 2 * Math.PI + phase * wave.speed + wave.offset);
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    frame = requestAnimationFrame(draw);
  }

  /** Feed a 0..1 microphone level. */
  function setLevel(rms) {
    target = Math.max(0, Math.min(1, Number(rms) || 0));
    lastLevelAt = performance.now();
  }

  /** Flatten and stop the waves (true) or let them follow the voice again. */
  function setFrozen(next) {
    frozen = Boolean(next);
    root.classList.toggle("is-frozen", frozen);
  }

  window.addEventListener("resize", resize);
  // --wave-color only resolves once the canvas is in the document.
  requestAnimationFrame(() => {
    if (disposed) return;
    resize();
    frame = requestAnimationFrame(draw);
  });

  function dispose() {
    disposed = true;
    window.removeEventListener("resize", resize);
    if (frame != null) cancelAnimationFrame(frame);
    frame = null;
  }

  return { root, setLevel, setFrozen, dispose };
}
