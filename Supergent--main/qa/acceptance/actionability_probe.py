"""Passive, bounded QA observations; never changes click semantics or UI CSS.

Install on an owned Playwright page only. The probe neither reads chat content
nor collects credentials. A failed action remains failed even if diagnostics
later see a visible element. CDP's runtime timeout bounds synchronous JS work;
it cannot guarantee host scheduling while Windows or the browser is suspended.
"""
from __future__ import annotations

import time

PROBE_KEY = "__wiseOwnedQaActionability"
INSTALL = r"""() => {
  const key = '__wiseOwnedQaActionability';
  if (window[key]) return;
  const p = window[key] = {frames: 0, ticks: 0, lastFrame: null,
    lastTick: performance.now(), maxTickGap: 0, lastVisibility: document.visibilityState,
    visibilityEvents: [], frameGaps: [], timerGaps: []};
  const push = (list, value) => { list.push(value); if (list.length > 12) list.shift(); };
  function frame(now) {
    if (p.lastFrame !== null) push(p.frameGaps, Math.round(now - p.lastFrame));
    p.lastFrame = now; p.frames++; p.raf = requestAnimationFrame(frame);
  }
  p.raf = requestAnimationFrame(frame);
  p.timer = setInterval(() => {
    const now = performance.now(), gap = now - p.lastTick;
    p.maxTickGap = Math.max(p.maxTickGap, gap); push(p.timerGaps, Math.round(gap));
    p.lastTick = now; p.ticks++;
  }, 500);
  p.visibilityHandler = () => {
    p.lastVisibility = document.visibilityState;
    push(p.visibilityEvents, {state: document.visibilityState, at: performance.now()});
  };
  document.addEventListener('visibilitychange', p.visibilityHandler);
}"""

SNAPSHOT = r"""(selector) => {
  const now = performance.now(), p = window.__wiseOwnedQaActionability;
  const e = document.querySelector(selector), box = e?.getBoundingClientRect();
  const style = e ? getComputedStyle(e) : null;
  const center = box ? {x: box.x + box.width / 2, y: box.y + box.height / 2} : null;
  const hit = center ? document.elementFromPoint(center.x, center.y) : null;
  const identity = n => n ? {tag: n.tagName, id: n.id || null} : null;
  const animations = e ? e.getAnimations({subtree: true}).slice(0, 16).map(a => ({
    playState: a.playState, currentTime: a.currentTime,
    timing: a.effect?.getComputedTiming ? (() => {
      const t = a.effect.getComputedTiming(); return {duration: String(t.duration),
        iterations: String(t.iterations), progress: t.progress, endTime: String(t.endTime)};
    })() : null
  })) : [];
  return {schema: 'wise.actionability-observation.v1', selector, visibility: document.visibilityState,
    hasFocus: document.hasFocus(), readyState: document.readyState, browserNow: now,
    viewport: {width: innerWidth, height: innerHeight, scrollX, scrollY},
    element: e ? {identity: identity(e), connected: e.isConnected, hidden: e.hidden,
      disabled: Boolean(e.disabled), ariaDisabled: e.getAttribute('aria-disabled'),
      box: {x: box.x, y: box.y, width: box.width, height: box.height},
      style: {display: style.display, visibility: style.visibility, opacity: style.opacity,
        transform: style.transform, pointerEvents: style.pointerEvents,
        animationName: style.animationName, transitionDuration: style.transitionDuration},
      hit: identity(hit), centerHitsElement: Boolean(hit && (hit === e || e.contains(hit))), animations} : null,
    heartbeat: p ? {frames: p.frames, ticks: p.ticks,
      lastFrameAgeMs: p.lastFrame === null ? null : now - p.lastFrame,
      lastTickAgeMs: now - p.lastTick, maxTickGapMs: p.maxTickGap,
      frameGaps: p.frameGaps.slice(), timerGaps: p.timerGaps.slice(),
      visibilityEvents: p.visibilityEvents.slice()} : null};
}"""


def install_probe(page):
    """Install once now and in subsequent owned-page navigations."""
    page.add_init_script("(" + INSTALL + ")()")
    page.evaluate(INSTALL)


def observe_actionability(page, selector="#settingsButton"):
    """Best-effort synchronous snapshot; never replaces or retries an action."""
    if selector not in {"#settingsButton", "#closeSettings", "#prompt"}:
        raise ValueError("Only fixed owned UI selectors are admitted")
    import json
    started = time.monotonic()
    session = None
    try:
        session = page.context.new_cdp_session(page)
        result = session.send("Runtime.evaluate", {"expression": "(" + SNAPSHOT + ")(" + json.dumps(selector) + ")",
            "returnByValue": True, "awaitPromise": False, "timeout": 1500})
        if result.get("exceptionDetails"):
            raise RuntimeError("Diagnostic JavaScript did not complete within its runtime bound")
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError("Diagnostic returned no structured observation")
        return {"status": "OBSERVED", **value, "host_elapsed_s": round(time.monotonic() - started, 4)}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "selector": selector, "error_type": type(exc).__name__,
            "error": str(exc)[:500], "host_elapsed_s": round(time.monotonic() - started, 4)}
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass
