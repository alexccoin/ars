/* ==========================================================================
   A.R.S — vitals
   services/gateway/src/ars_gateway/ui/panels/vitals.js

   A personal medical record, not a dashboard. One card per measure, a small
   legible chart, the latest number large, and — where A.R.S has a source for a
   range — the range drawn as a band so "outside it" is something you see rather
   than something you are told.

   WHAT THIS PANEL IS ALLOWED TO SAY
   `packages/protocol/src/ars_protocol/health.py` decides that, and it decides it
   in the type system: there is a reading, a reference range, and the observation
   that one sits outside the other. There is no Condition, no Assessment, no
   Recommendation, "because a type is an invitation". This panel is where that
   restraint becomes visible or gets lost, so:

     · every out-of-range finding is rendered together with the citing source,
       verbatim, never abbreviated and never behind a tooltip;
     · the words diagnosis / condition / risk appear nowhere in the UI copy, in
       any of the three languages, and neither does any phrasing that reads as a
       clinical judgement;
     · out-of-range is marked in --ars-warn, not --ars-danger. Amber says "look
       at this". Red says "you are in trouble", which is a claim about a person
       that no measurement supports;
     · trend is drawn in a neutral colour on purpose. Rising steps and rising
       systolic are the same arrow and A.R.S has no business colouring one green.

   RENDERING
   SVG, not canvas. Thirty points per series, five series: there is no animation
   loop to run, so the steady-state cost of this panel is exactly zero frames —
   which matters more here than anywhere, because the GPU is usually busy with a
   local model. The only motion is a one-shot line draw-in per card, skipped
   under prefers-reduced-motion and skipped when the tab is not on screen. The
   poll stops when the deck tab is hidden or the document is hidden.

   Styling is injected once at import from a token-only string, same pattern as
   deck.js and entity/hud.js. No build step, no literal colour.
   ========================================================================== */

import { t } from './i18n.js';

/* The ten kinds, in the order `VitalKind` declares them — a personal record
   wants a stable order, not a most-recently-touched one.

   DUPLICATION, KNOWN: this list and the unit fallbacks below restate what
   `VitalKind` already knows. The add-form needs the kinds before any reading of
   that kind exists, and `GET /api/health/readings` only reports units for kinds
   that have data, so there is nothing to read them from. `GET /api/health/kinds`
   -> [{kind, unit, plausible:[low,high]}] would delete both of these constants
   and let the form show the store's own plausibility bounds; asked for in the
   report rather than added here. Units that arrive from the server always win
   over this table. */
const KINDS = [
  'heart_rate', 'bp_systolic', 'bp_diastolic', 'spo2', 'body_temperature',
  'blood_glucose', 'weight', 'respiratory_rate', 'steps', 'sleep_minutes',
];

const WINDOWS = [7, 30, 90];

const VITALS_CSS = `
.ars-vitals { display: flex; flex-direction: column; gap: var(--ars-space-3); }
/* The shell caps every panel body at 40vh with its own scroller. That is right
   for a five-row list and wrong for a record you read top to bottom, so this one
   panel grows and lets the deck's single scroller do the work. */
#panel-vitals .hud-panel__body { max-height: none; overflow: visible; }

.ars-vitals__intro {
  margin: 0;
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-body);
  color: var(--ars-text-muted);
}
.ars-vitals__bar {
  display: flex; align-items: center; justify-content: space-between; gap: var(--ars-space-2);
  flex-wrap: wrap;
}
.ars-vitals__windows {
  display: inline-flex; padding: 2px; gap: 2px;
  background: var(--ars-surface-sunken);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-sm);
  box-shadow: var(--ars-inner-well);
}
.ars-vitals__window {
  padding: 2px var(--ars-space-2);
  background: transparent; border: none;
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-2xs);
  font-variant-numeric: tabular-nums;
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
  transition: color var(--ars-dur-fast) var(--ars-ease-mech),
              background var(--ars-dur-fast) var(--ars-ease-mech);
}
.ars-vitals__window:hover { color: var(--ars-text-secondary); background: var(--ars-surface-hover); }
.ars-vitals__window[aria-pressed="true"] {
  color: var(--ars-text-accent);
  background: var(--ars-surface-raised);
  box-shadow: var(--ars-glow-xs);
}
.ars-vitals__summary {
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-2xs);
  font-variant-numeric: tabular-nums;
  color: var(--ars-text-muted);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
}
.ars-vitals__msgs { display: flex; flex-direction: column; gap: var(--ars-space-1); }
.ars-vitals__msg {
  padding: var(--ars-space-1) var(--ars-space-2);
  border-left: 2px solid var(--ars-accent);
  background: var(--ars-surface-raised);
  border-radius: var(--ars-radius-xs);
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-snug);
  color: var(--ars-text-secondary);
}
.ars-vitals__msg[data-kind="warn"]  { border-left-color: var(--ars-warn); }
.ars-vitals__msg[data-kind="error"] { border-left-color: var(--ars-danger); color: var(--ars-text); }

/* ------------------------------------------------------------ section shell */
.ars-vitals__sec {
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-md);
  background: var(--ars-surface);
  overflow: hidden;
}
.ars-vitals__sec--outside { border-color: color-mix(in srgb, var(--ars-warn) 40%, transparent); }
.ars-vitals__sec-head {
  display: flex; align-items: baseline; gap: var(--ars-space-2);
  padding: var(--ars-space-2) var(--ars-space-3);
  background: var(--ars-surface-raised);
  border-bottom: var(--ars-hairline) solid var(--ars-border);
}
.ars-vitals__sec-title {
  margin: 0;
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-warn);
}
.ars-vitals__sec-title--add { color: var(--ars-text-accent); }
.ars-vitals__sec-count {
  margin-left: auto;
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-2xs);
  font-variant-numeric: tabular-nums;
  color: var(--ars-text-muted);
}
.ars-vitals__sec-body { padding: var(--ars-space-2) var(--ars-space-3) var(--ars-space-3); }
.ars-vitals__disclaimer {
  margin: 0 0 var(--ars-space-2);
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-snug);
  color: var(--ars-text-muted);
}

/* ------------------------------------------------------------- finding group */
.ars-vitals__groups { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--ars-space-3); }
.ars-vitals__group + .ars-vitals__group { border-top: var(--ars-hairline) dotted var(--ars-border); padding-top: var(--ars-space-3); }
.ars-vitals__group-head { display: flex; align-items: baseline; gap: var(--ars-space-2); flex-wrap: wrap; }
.ars-vitals__group-kind {
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-sm);
  font-weight: var(--ars-weight-semi);
  color: var(--ars-text);
}
.ars-vitals__group-what {
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-xs);
  font-variant-numeric: tabular-nums;
  color: var(--ars-warn);
}
/* The citation. Not decoration: A.R.S may say a number is outside a named
   range, and this is the name. It is always on screen, never truncated. */
.ars-vitals__cite {
  margin: var(--ars-space-1) 0 var(--ars-space-2);
  padding: var(--ars-space-1) var(--ars-space-2);
  border-left: 2px solid var(--ars-border-strong);
  background: var(--ars-surface-sunken);
  border-radius: var(--ars-radius-xs);
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-snug);
  color: var(--ars-text-secondary);
}
/* The source's own words, quoted. Deliberately NOT translated: this is a
   citation from an English-language clinical reference, and machine-paraphrasing
   one into Romanian or German would be inventing authority. The quote marks say
   whose sentence it is. */
.ars-vitals__cite-note::before { content: '“'; }
.ars-vitals__cite-note::after  { content: '”'; }
.ars-vitals__cite-note {
  display: block; margin-top: 2px;
  font-size: var(--ars-text-2xs);
  color: var(--ars-text-muted);
  font-style: italic;
}
.ars-vitals__more {
  margin-top: var(--ars-space-1);
  padding: 2px var(--ars-space-2);
  background: transparent;
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  font-size: var(--ars-text-2xs);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
}
.ars-vitals__more:hover { color: var(--ars-text-accent); border-color: var(--ars-border-strong); }

/* ------------------------------------------------------------------- rows */
.ars-vitals__rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.ars-vitals__row-item { border-bottom: var(--ars-hairline) dotted var(--ars-border); }
.ars-vitals__row-item:last-child { border-bottom: none; }
.ars-vitals__row-item[data-flash="1"] { background: color-mix(in srgb, var(--ars-accent) 16%, transparent); }
/* 300px of column. A row that wraps four competing spans is unreadable, so:
   number and moment on one line, the user's own words on the next. */
.ars-vitals__row {
  display: grid;
  grid-template-columns: auto auto 1fr auto;
  align-items: baseline;
  column-gap: var(--ars-space-2);
  padding: 4px 0;
  font-size: var(--ars-text-xs);
}
.ars-vitals__row-val {
  grid-column: 1; grid-row: 1;
  font-variant-numeric: tabular-nums;
  font-weight: var(--ars-weight-semi);
  color: var(--ars-text);
  white-space: nowrap;
}
.ars-vitals__row-item[data-outside="1"] .ars-vitals__row-val { color: var(--ars-warn); }
.ars-vitals__row-mark { grid-column: 2; grid-row: 1; color: var(--ars-warn); font-size: var(--ars-text-2xs); }
.ars-vitals__row-when { grid-column: 3; grid-row: 1; color: var(--ars-text-muted); font-size: var(--ars-text-2xs); white-space: nowrap; }
.ars-vitals__row-note {
  grid-column: 1 / -1; grid-row: 2; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--ars-text-secondary); font-style: italic;
  font-size: var(--ars-text-2xs);
}
.ars-vitals__del {
  grid-column: 4; grid-row: 1; align-self: center;
  padding: 1px var(--ars-space-1);
  background: transparent; border: var(--ars-hairline) solid transparent;
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  cursor: pointer; line-height: 1;
  transition: color var(--ars-dur-fast) var(--ars-ease-mech),
              border-color var(--ars-dur-fast) var(--ars-ease-mech);
}
.ars-vitals__del svg { width: 12px; height: 12px; display: block; }
.ars-vitals__del:hover { color: var(--ars-danger); border-color: color-mix(in srgb, var(--ars-danger) 55%, transparent); }

/* Deletion is a non-negotiable in this project, so it does not hide behind a
   browser alert that says "127.0.0.1 says". It opens in place, spells out the
   exact number and moment that is about to leave the store, and says that it
   leaves for good. Escape keeps it. */
.ars-vitals__confirm {
  display: flex; flex-direction: column; gap: var(--ars-space-2);
  padding: var(--ars-space-2);
  margin: 2px 0;
  background: color-mix(in srgb, var(--ars-danger) 12%, var(--ars-surface-raised));
  border: var(--ars-hairline) solid color-mix(in srgb, var(--ars-danger) 55%, transparent);
  border-radius: var(--ars-radius-sm);
}
.ars-vitals__confirm-actions { display: flex; gap: var(--ars-space-2); justify-content: flex-end; }
.ars-vitals__confirm-text {
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-snug);
  color: var(--ars-text);
}
.ars-vitals__confirm-yes, .ars-vitals__confirm-no {
  flex: none;
  padding: 3px var(--ars-space-2);
  border-radius: var(--ars-radius-xs);
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
}
.ars-vitals__confirm-yes {
  background: var(--ars-danger-fill);
  border: var(--ars-hairline) solid var(--ars-danger);
  color: var(--ars-text-on-danger);
}
.ars-vitals__confirm-no {
  background: transparent;
  border: var(--ars-hairline) solid var(--ars-border-strong);
  color: var(--ars-text-secondary);
}
.ars-vitals__confirm-no:hover { color: var(--ars-text); background: var(--ars-surface-hover); }

/* -------------------------------------------------------------------- cards */
.ars-vitals__cards { display: flex; flex-direction: column; gap: var(--ars-space-3); }
.ars-vitals__card {
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-md);
  background: var(--ars-panel-bg);
  box-shadow: var(--ars-inner-top);
  padding: var(--ars-space-2) var(--ars-space-3) var(--ars-space-2);
}
.ars-vitals__card-head { display: flex; align-items: baseline; gap: var(--ars-space-2); }
.ars-vitals__card-kind {
  margin: 0;
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-text-muted);
}
.ars-vitals__trend {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 3px;
  font-size: var(--ars-text-2xs);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  /* Neutral on purpose. See the header comment. */
  color: var(--ars-text-muted);
}
.ars-vitals__trend-arrow { font-family: var(--ars-font-mono); font-size: var(--ars-text-xs); }

.ars-vitals__big { display: flex; align-items: baseline; gap: var(--ars-space-2); margin-top: 2px; }
.ars-vitals__value {
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-3xl);
  font-weight: var(--ars-weight-bold);
  line-height: var(--ars-leading-tight);
  letter-spacing: var(--ars-track-tight);
  font-variant-numeric: tabular-nums;
  color: var(--ars-text-accent);
  text-shadow: var(--ars-text-glow);
}
.ars-vitals__card[data-latest-outside="1"] .ars-vitals__value {
  color: var(--ars-warn);
  text-shadow: var(--ars-glow-warn);
}
.ars-vitals__unit { font-size: var(--ars-text-sm); color: var(--ars-text-secondary); }
.ars-vitals__when {
  margin-left: auto;
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-2xs);
  color: var(--ars-text-muted);
  text-align: right;
}
.ars-vitals__chart { display: block; width: 100%; height: auto; margin: var(--ars-space-1) 0 2px; }
.ars-vitals__foot {
  display: flex; align-items: center; gap: var(--ars-space-2); flex-wrap: wrap;
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-2xs);
  font-variant-numeric: tabular-nums;
  color: var(--ars-text-muted);
}
.ars-vitals__toggle {
  margin-left: auto;
  padding: 1px var(--ars-space-2);
  background: transparent;
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-xs);
  color: var(--ars-text-muted);
  font-family: inherit; font-size: var(--ars-text-2xs);
  letter-spacing: var(--ars-track-label); text-transform: uppercase;
  cursor: pointer;
}
.ars-vitals__toggle:hover { color: var(--ars-text-accent); border-color: var(--ars-border-strong); }
.ars-vitals__card-rows { margin-top: var(--ars-space-2); max-height: 190px; overflow-y: auto; scrollbar-width: thin; }

/* --------------------------------------------------------------- chart parts */
.ars-vitals__band { fill: var(--ars-ok); opacity: 0.08; }
.ars-vitals__band-edge { stroke: var(--ars-ok); stroke-opacity: 0.45; stroke-width: 0.6; stroke-dasharray: 3 3; }
.ars-vitals__band-label { fill: var(--ars-text-muted); font-size: 7px; font-family: var(--ars-font-mono); }
.ars-vitals__line { fill: none; stroke: var(--ars-accent); stroke-width: 1.4; stroke-linejoin: round; stroke-linecap: round; }
.ars-vitals__dot { fill: var(--ars-accent-bright); }
.ars-vitals__dot--out { fill: var(--ars-warn); stroke: var(--ars-bg-chrome); stroke-width: 0.8; }
.ars-vitals__dot--last { fill: var(--ars-accent-bright); stroke: var(--ars-bg-chrome); stroke-width: 1; }
.ars-vitals__card[data-latest-outside="1"] .ars-vitals__dot--last { fill: var(--ars-warn); }
.ars-vitals__ring { fill: none; stroke: var(--ars-accent); stroke-opacity: 0.55; stroke-width: 0.8; }
.ars-vitals__card[data-latest-outside="1"] .ars-vitals__ring { stroke: var(--ars-warn); }
.ars-vitals__xlabel { fill: var(--ars-text-muted); font-size: 7px; font-family: var(--ars-font-mono); }
.ars-vitals__hit { fill: transparent; cursor: pointer; }

@media (prefers-reduced-motion: no-preference) {
  .ars-vitals__chart[data-draw="1"] .ars-vitals__line {
    stroke-dasharray: var(--ars-vitals-len);
    stroke-dashoffset: var(--ars-vitals-len);
    animation: ars-vitals-draw var(--ars-dur-slower) var(--ars-ease-out) forwards;
    animation-delay: var(--ars-vitals-delay, 0ms);
  }
  .ars-vitals__chart[data-draw="1"] .ars-vitals__dot,
  .ars-vitals__chart[data-draw="1"] .ars-vitals__ring {
    opacity: 0;
    animation: ars-vitals-fade var(--ars-dur-base) var(--ars-ease-out) forwards;
    animation-delay: calc(var(--ars-vitals-delay, 0ms) + var(--ars-dur-slower));
  }
}
@keyframes ars-vitals-draw { to { stroke-dashoffset: 0; } }
@keyframes ars-vitals-fade { to { opacity: 1; } }

/* A locked record must not look like an empty one. Amber for 428 (nobody has
   decided yet), red for 403 (decided, no) — the same two colours the guard uses
   everywhere else in this shell. */
.ars-vitals__blocked {
  border: var(--ars-hairline) solid color-mix(in srgb, var(--ars-guard-confirm) 55%, transparent);
  border-radius: var(--ars-radius-md);
  background: color-mix(in srgb, var(--ars-guard-confirm) 8%, var(--ars-surface));
  padding: var(--ars-space-3);
  display: flex; flex-direction: column; gap: var(--ars-space-2);
}
.ars-vitals__blocked[data-status="403"] {
  border-color: color-mix(in srgb, var(--ars-guard-deny) 55%, transparent);
  background: color-mix(in srgb, var(--ars-guard-deny) 8%, var(--ars-surface));
}
.ars-vitals__blocked-head { display: flex; align-items: center; gap: var(--ars-space-2); }
.ars-vitals__blocked-head svg { width: 15px; height: 15px; flex: none; color: var(--ars-guard-confirm); }
.ars-vitals__blocked[data-status="403"] .ars-vitals__blocked-head svg { color: var(--ars-guard-deny); }
.ars-vitals__blocked-title {
  margin: 0;
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-guard-confirm);
}
.ars-vitals__blocked[data-status="403"] .ars-vitals__blocked-title { color: var(--ars-guard-deny); }
.ars-vitals__blocked-lead {
  margin: 0;
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-body);
  color: var(--ars-text-secondary);
}
/* The guard's own sentence, already written in the reader's language. Quoted
   rather than paraphrased: this panel does not get to restate a refusal. */
.ars-vitals__blocked-said {
  margin: 0;
  font-size: var(--ars-text-2xs);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-text-muted);
}
.ars-vitals__blocked-quote {
  margin: 0;
  padding: var(--ars-space-2);
  border-left: 2px solid var(--ars-border-strong);
  background: var(--ars-surface-sunken);
  border-radius: var(--ars-radius-xs);
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-snug);
  color: var(--ars-text);
}
.ars-vitals__blocked-actions { display: flex; gap: var(--ars-space-2); flex-wrap: wrap; align-items: center; }
.ars-vitals__blocked-where { font-size: var(--ars-text-2xs); color: var(--ars-text-muted); flex: 1 1 100%; }

.ars-vitals__empty {
  margin: 0; padding: var(--ars-space-4) var(--ars-space-3);
  text-align: center;
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-body);
  color: var(--ars-text-muted);
  border: var(--ars-hairline) dashed var(--ars-border);
  border-radius: var(--ars-radius-md);
}

/* ---------------------------------------------------------------- add form */
.ars-vitals__add { display: flex; flex-direction: column; gap: var(--ars-space-2); }
.ars-vitals__add-hint {
  margin: 0;
  font-size: var(--ars-text-xs);
  line-height: var(--ars-leading-snug);
  color: var(--ars-text-muted);
}
.ars-vitals__field { display: flex; flex-direction: column; gap: 2px; }
.ars-vitals__field-label {
  font-size: var(--ars-text-2xs);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  color: var(--ars-text-muted);
}
.ars-vitals__add select,
.ars-vitals__add input {
  width: 100%;
  padding: 5px var(--ars-space-2);
  background: var(--ars-surface-sunken);
  border: var(--ars-hairline) solid var(--ars-border);
  border-radius: var(--ars-radius-sm);
  box-shadow: var(--ars-inner-well);
  color: var(--ars-text);
  font-family: var(--ars-font-ui);
  font-size: var(--ars-text-sm);
}
.ars-vitals__add input { font-family: var(--ars-font-mono); font-variant-numeric: tabular-nums; }
.ars-vitals__add select:focus-visible,
.ars-vitals__add input:focus-visible,
.ars-vitals__window:focus-visible,
.ars-vitals__toggle:focus-visible,
.ars-vitals__more:focus-visible,
.ars-vitals__del:focus-visible,
.ars-vitals__confirm-yes:focus-visible,
.ars-vitals__confirm-no:focus-visible,
.ars-vitals__submit:focus-visible { outline: none; box-shadow: var(--ars-focus-ring); }
.ars-vitals__row-2 { display: flex; gap: var(--ars-space-2); align-items: flex-end; }
.ars-vitals__row-2 .ars-vitals__field { flex: 1 1 auto; min-width: 0; }
.ars-vitals__value-wrap { position: relative; }
.ars-vitals__value-wrap input { padding-right: 4.6em; }
.ars-vitals__value-unit {
  position: absolute; right: var(--ars-space-2); top: 50%; transform: translateY(-50%);
  pointer-events: none;
  font-family: var(--ars-font-mono);
  font-size: var(--ars-text-xs);
  color: var(--ars-text-muted);
}
.ars-vitals__submit {
  flex: none;
  padding: 6px var(--ars-space-3);
  background: var(--ars-surface-raised);
  border: var(--ars-hairline) solid var(--ars-border-accent);
  border-radius: var(--ars-radius-sm);
  color: var(--ars-text-accent);
  font-family: var(--ars-font-display);
  font-size: var(--ars-text-2xs);
  font-weight: var(--ars-weight-semi);
  letter-spacing: var(--ars-track-label);
  text-transform: uppercase;
  cursor: pointer;
  transition: background var(--ars-dur-fast) var(--ars-ease-mech),
              box-shadow var(--ars-dur-fast) var(--ars-ease-mech);
}
.ars-vitals__submit:hover:not(:disabled) { background: var(--ars-surface-hover); box-shadow: var(--ars-glow-sm); }
.ars-vitals__submit:disabled { opacity: 0.5; cursor: default; }
`;

const SVG_NS = 'http://www.w3.org/2000/svg';
const LOCK = '<rect x="2.6" y="6.4" width="9.8" height="7" rx="1.4"/><path d="M4.9 6.4V4.6a2.6 2.6 0 0 1 5.2 0v1.8"/><path d="M7.5 9.2v1.8"/>';
const TRASH = '<path d="M2.5 3.5h9M6 3.5V2h3v1.5M3.6 3.5l.6 8.2a1 1 0 0 0 1 .8h3.6a1 1 0 0 0 1-.8l.6-8.2"/><path d="M6 6v4M8 6v4"/>';

let injected = false;
function ensureStyles() {
  if (injected || typeof document === 'undefined') return;
  injected = true;
  const style = document.createElement('style');
  style.id = 'ars-vitals-style';
  style.textContent = VITALS_CSS;
  document.head.appendChild(style);
}

function el(tag, className, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    }
  }
  return node;
}

function svg(tag, className, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  if (className) node.setAttribute('class', className);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      node.setAttribute(k, String(v));
    }
  }
  return node;
}

function lineIcon(path, box) {
  const s = svg('svg', null, {
    viewBox: `0 0 ${box} ${box}`, fill: 'none', stroke: 'currentColor',
    'stroke-width': '1.1', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true',
  });
  s.innerHTML = path;
  return s;
}
function trashIcon() { return lineIcon(TRASH, 14); }
function lockIcon() { return lineIcon(LOCK, 15); }

export function createVitalsPanel({ root, hud, getLang, baseUrl = '', onOpenAccess = null }) {
  ensureStyles();

  /* ------------------------------------------------------------- formatting */

  const numFmt = new Map();
  function num(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const lang = getLang();
    let f = numFmt.get(lang);
    if (!f) { f = new Intl.NumberFormat(lang, { maximumFractionDigits: 1 }); numFmt.set(lang, f); }
    return f.format(v);
  }

  const dayFmt = new Map();
  const timeFmt = new Map();
  function fmtDay(ms) {
    const lang = getLang();
    let f = dayFmt.get(lang);
    if (!f) { f = new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'short' }); dayFmt.set(lang, f); }
    return f.format(new Date(ms));
  }
  function fmtTime(ms) {
    const lang = getLang();
    let f = timeFmt.get(lang);
    if (!f) { f = new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }); timeFmt.set(lang, f); }
    return f.format(new Date(ms));
  }
  /** "today 09:14" / "yesterday 08:02" / "12 Aug 08:02" — a personal record is
   *  read in relative time near the present and in dates further back. */
  function fmtWhen(ms) {
    const now = new Date();
    const then = new Date(ms);
    const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    const yest = new Date(now.getTime() - 86400000);
    if (sameDay(now, then)) return `${t('vitals.today', getLang())} ${fmtTime(ms)}`;
    if (sameDay(yest, then)) return `${t('vitals.yesterday', getLang())} ${fmtTime(ms)}`;
    return `${fmtDay(ms)} ${fmtTime(ms)}`;
  }

  function kindLabel(kind) { return t(`vitals.kind.${kind}`, getLang()); }
  /** The store's unit string is authoritative for what was measured; the i18n
   *  table only exists to say it in the reader's language (the store speaks
   *  English: "breaths/min"). Symbols like mmHg are identical either way. */
  function unitFor(kind, serverUnit) {
    const key = `vitals.unit.${kind}`;
    const localized = KINDS.includes(kind) ? t(key, getLang()) : null;
    return localized || serverUnit || '';
  }

  /* ----------------------------------------------------------------- chrome */

  let panelHandle;
  if (hud && typeof hud.panel === 'function') {
    try {
      panelHandle = hud.panel({ id: 'panel-vitals', title: t('vitals.title', getLang()) });
    } catch (err) {
      console.warn('[vitals] hud.panel failed, using plain container', err);
    }
  }
  if (!panelHandle) {
    const container = el('section', 'hud-panel', { id: 'panel-vitals', 'aria-label': t('vitals.title', getLang()) });
    const header = el('div', 'hud-panel__header');
    header.append(el('h2', 'hud-panel__title', { text: t('vitals.title', getLang()) }));
    const body = el('div', 'hud-panel__body');
    container.append(header, body);
    panelHandle = { root: container, body, setTitle(text) { header.firstChild.textContent = text; } };
  }
  root.append(panelHandle.root);

  const wrap = el('div', 'ars-vitals');
  const intro = el('p', 'ars-vitals__intro', { text: t('vitals.intro', getLang()) });
  const bar = el('div', 'ars-vitals__bar');
  const windows = el('div', 'ars-vitals__windows', { role: 'group', 'aria-label': t('vitals.window.label', getLang()) });
  const summary = el('span', 'ars-vitals__summary');
  bar.append(windows, summary);
  const msgs = el('div', 'ars-vitals__msgs', { role: 'status', 'aria-live': 'polite' });
  const blockedMount = el('div');
  const outsideMount = el('div');
  const cards = el('div', 'ars-vitals__cards');
  const emptyNote = el('p', 'ars-vitals__empty', { text: t('vitals.empty', getLang()) });
  const addMount = el('div');
  wrap.append(intro, bar, msgs, blockedMount, outsideMount, cards, emptyNote, addMount);
  panelHandle.body.append(wrap);

  let days = 30;
  const windowBtns = new Map();
  for (const d of WINDOWS) {
    const b = el('button', 'ars-vitals__window', {
      type: 'button', 'aria-pressed': String(d === days),
      text: t(`vitals.window.${d}.short`, getLang()),
      title: t(`vitals.window.${d}`, getLang()),
    });
    b.addEventListener('click', () => {
      if (days === d) return;
      days = d;
      for (const [k, btn] of windowBtns) btn.setAttribute('aria-pressed', String(k === d));
      refresh({ animate: true });
    });
    windowBtns.set(d, b);
    windows.append(b);
  }

  /* ------------------------------------------------------------------ state */

  let payload = { days: 30, series: {}, outside_range: [] };
  // A guard verdict on the read path: { status: 428|403, detail }. Distinct from
  // `payload.series` being empty, which means "no readings", and from `offline`,
  // which means "no gateway". Three different blank screens, three different
  // sentences.
  let block = null;
  let offline = false;
  let expandedRows = new Set();   // kinds whose full reading list is open
  let expandedGroups = new Set(); // kinds whose finding list is fully shown
  let allowDraw = true;           // one-shot line animation, only when on screen

  /** FastAPI puts the guard's own explanation in `detail`. It is already written
   *  in the user's language by the time it gets here, so it is quoted, never
   *  rewritten. */
  async function guardDetail(res) {
    try {
      const body = await res.json();
      if (!body) return '';
      if (typeof body.detail === 'string') return body.detail;
      if (body.detail) return JSON.stringify(body.detail);
      return '';
    } catch { return ''; }
  }

  function say(text, kind = 'info') {
    const line = el('div', 'ars-vitals__msg', { text, 'data-kind': kind });
    msgs.append(line);
    setTimeout(() => line.remove(), 9000);
  }

  /* ------------------------------------------------------------------- chart */

  const W = 300;
  const H = 78;
  const PAD = { l: 3, r: 34, t: 12, b: 13 };

  /**
   * A small legible chart. Time on x across the whole selected window (so a gap
   * in the record reads as a gap, not as a compressed run of points); value on
   * y, with the cited reference range drawn as a band when one is known.
   */
  function buildChart(kind, entry, range, outsideById) {
    const readings = [...entry.readings].sort((a, b) => a.measured_at_ms - b.measured_at_ms);
    const node = svg('svg', 'ars-vitals__chart', {
      viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet',
      role: 'img', focusable: 'false',
    });
    if (!readings.length) return { node, click: null };

    const t1 = Date.now();
    const t0 = t1 - days * 86400000;
    const values = readings.map((r) => r.value);
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (range) {
      if (range.low !== null && range.low !== undefined) lo = Math.min(lo, range.low);
      if (range.high !== null && range.high !== undefined) hi = Math.max(hi, range.high);
    }
    let span = hi - lo;
    if (span < 1e-6) { span = Math.max(Math.abs(hi) * 0.1, 1); lo -= span / 2; hi += span / 2; }
    lo -= span * 0.12; hi += span * 0.12;

    const plotW = W - PAD.l - PAD.r;
    const x = (ms) => PAD.l + Math.min(1, Math.max(0, (ms - t0) / Math.max(1, t1 - t0))) * plotW;
    const y = (v) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);

    // reference band first, so the line sits on top of it
    if (range) {
      const yTop = range.high === null || range.high === undefined ? PAD.t : y(range.high);
      const yBot = range.low === null || range.low === undefined ? H - PAD.b : y(range.low);
      node.append(svg('rect', 'ars-vitals__band', {
        x: PAD.l, y: Math.min(yTop, yBot), width: plotW, height: Math.abs(yBot - yTop),
      }));
      for (const [v, yy] of [[range.high, yTop], [range.low, yBot]]) {
        if (v === null || v === undefined) continue;
        node.append(svg('line', 'ars-vitals__band-edge', { x1: PAD.l, x2: W - PAD.r, y1: yy, y2: yy }));
      }
      // Two short lines, not one long one: the gutter is 30 units wide and
      // "cited 60-100" on one line ran off the edge of the card.
      const mid = (Math.min(yTop, yBot) + Math.max(yTop, yBot)) / 2;
      const label = svg('text', 'ars-vitals__band-label', { x: W - PAD.r + 4, y: mid - 1 });
      const l1 = svg('tspan', null, { x: W - PAD.r + 4 });
      l1.textContent = t('vitals.band.word', getLang());
      const l2 = svg('tspan', null, { x: W - PAD.r + 4, dy: '8' });
      l2.textContent = `${num(range.low)}–${num(range.high)}`;
      label.append(l1, l2);
      const title = svg('title');
      title.textContent = t('vitals.outside.source', getLang(), { source: range.source });
      label.append(title);
      node.append(label);
    }

    if (readings.length > 1) {
      const d = readings.map((r, i) => `${i ? 'L' : 'M'}${x(r.measured_at_ms).toFixed(2)} ${y(r.value).toFixed(2)}`).join(' ');
      const path = svg('path', 'ars-vitals__line', { d });
      node.append(path);
      // measured once here so the draw-in animation has a real dash length
      // rather than a guessed one; getTotalLength is only valid once attached,
      // so it is set by the caller after mount.
      path.dataset.needsLength = '1';
    }

    const hits = [];
    readings.forEach((r, i) => {
      const cx = x(r.measured_at_ms);
      const cy = y(r.value);
      const out = outsideById.has(r.id);
      const last = i === readings.length - 1;
      if (last) node.append(svg('circle', 'ars-vitals__ring', { cx, cy, r: 4.4 }));
      node.append(svg('circle', out ? 'ars-vitals__dot ars-vitals__dot--out' : (last ? 'ars-vitals__dot ars-vitals__dot--last' : 'ars-vitals__dot'), {
        cx, cy, r: out ? 2.5 : (last ? 2.6 : 1.6),
      }));
      const hit = svg('circle', 'ars-vitals__hit', { cx, cy, r: 6 });
      const title = svg('title');
      title.textContent = `${num(r.value)} ${unitFor(kind, entry.unit)} · ${fmtWhen(r.measured_at_ms)}${r.note ? ` · ${r.note}` : ''}`;
      hit.append(title);
      hit.dataset.id = r.id;
      hits.push(hit);
    });
    for (const h of hits) node.append(h); // hit targets on top

    const xa = svg('text', 'ars-vitals__xlabel', { x: PAD.l, y: H - 3 });
    xa.textContent = fmtDay(t0);
    const xb = svg('text', 'ars-vitals__xlabel', { x: W - PAD.r, y: H - 3, 'text-anchor': 'end' });
    xb.textContent = fmtDay(t1);
    node.append(xa, xb);

    const agg = entry.aggregate || {};
    node.setAttribute('aria-label', t('vitals.chart.aria', getLang(), {
      kind: kindLabel(kind), n: readings.length, days,
      min: num(agg.minimum), max: num(agg.maximum), unit: unitFor(kind, entry.unit),
      trend: t(`vitals.trend.${agg.trend || 'unknown'}`, getLang()),
    }));
    return { node };
  }

  /* -------------------------------------------------------------------- rows */

  /** One reading, with a delete that opens in place. */
  function readingRow(reading, { unit, finding, mark = true }) {
    const li = el('li', 'ars-vitals__row-item');
    if (finding) li.dataset.outside = '1';
    li.dataset.id = reading.id;

    const main = el('div', 'ars-vitals__row');
    main.append(el('span', 'ars-vitals__row-val', { text: `${num(reading.value)} ${unit}` }));
    main.append(el('span', 'ars-vitals__row-when', { text: fmtWhen(reading.measured_at_ms) }));
    // In the findings list the group header already says "4 below 60-100 bpm";
    // repeating it per row is noise. In a card's full list it is the only thing
    // that says so, and there it is a caret with the sentence on hover.
    if (finding && mark) {
      const word = t(`vitals.outside.mark.${finding.direction === 'below' ? 'below' : 'above'}`, getLang());
      main.append(el('span', 'ars-vitals__row-mark', {
        text: finding.direction === 'below' ? '▼' : '▲', title: word, 'aria-label': word,
      }));
    }
    if (reading.note) main.append(el('span', 'ars-vitals__row-note', { text: reading.note }));

    const whenText = fmtWhen(reading.measured_at_ms);
    const del = el('button', 'ars-vitals__del', {
      type: 'button',
      title: t('vitals.delete', getLang()),
      'aria-label': t('vitals.delete.aria', getLang(), { value: num(reading.value), unit, when: whenText }),
    });
    del.append(trashIcon());
    main.append(del);
    li.append(main);

    const confirm = el('div', 'ars-vitals__confirm');
    confirm.hidden = true;
    confirm.append(el('span', 'ars-vitals__confirm-text', {
      text: t('vitals.delete.ask', getLang(), { value: num(reading.value), unit, when: whenText }),
    }));
    const yes = el('button', 'ars-vitals__confirm-yes', { type: 'button', text: t('vitals.delete.confirm', getLang()) });
    const no = el('button', 'ars-vitals__confirm-no', { type: 'button', text: t('vitals.delete.cancel', getLang()) });
    const actions = el('div', 'ars-vitals__confirm-actions');
    // Safe action first, destructive last, and the focus lands on "Keep":
    // Enter on an irreversible delete should not be one keystroke away.
    actions.append(no, yes);
    confirm.append(actions);
    li.append(confirm);

    function open(v) {
      confirm.hidden = !v;
      main.hidden = v;
      if (v) no.focus(); else del.focus();
    }
    del.addEventListener('click', () => open(true));
    no.addEventListener('click', () => open(false));
    confirm.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); open(false); } });
    yes.addEventListener('click', async () => {
      yes.disabled = true; no.disabled = true;
      const result = await destroyReading(reading.id);
      if (result.ok) {
        say(t('vitals.delete.done', getLang(), { value: num(reading.value), unit, when: whenText }));
        await refresh();
        return;
      }
      yes.disabled = false; no.disabled = false;
      if (result.status === 428 || result.status === 403) {
        const denied = result.status === 403;
        say(t(denied ? 'vitals.blocked.delete_denied' : 'vitals.blocked.delete', getLang()), denied ? 'error' : 'warn');
        if (result.detail) say(result.detail, 'warn');
        open(false);
      } else {
        say(t('vitals.delete.failed', getLang()), 'error');
      }
    });
    return li;
  }

  /* ---------------------------------------------------------------- sections */

  function renderOutside() {
    outsideMount.textContent = '';
    outsideMount.hidden = true;
    const findings = payload.outside_range || [];
    const total = Object.values(payload.series || {}).reduce((n, s) => n + (s.readings || []).length, 0);
    if (!total) return;

    const sec = el('section', 'ars-vitals__sec ars-vitals__sec--outside');
    const head = el('div', 'ars-vitals__sec-head');
    head.append(el('h3', 'ars-vitals__sec-title', { text: t('vitals.outside.title', getLang()) }));
    head.append(el('span', 'ars-vitals__sec-count', {
      text: t('vitals.outside.count', getLang(), { n: findings.length, total }),
    }));
    const body = el('div', 'ars-vitals__sec-body');
    sec.append(head, body);

    outsideMount.hidden = false;
    body.append(el('p', 'ars-vitals__disclaimer', { text: t('vitals.outside.disclaimer', getLang()) }));

    if (!findings.length) {
      body.append(el('p', 'ars-vitals__disclaimer', { text: t('vitals.outside.none', getLang()) }));
      outsideMount.append(sec);
      return;
    }

    // group by kind + direction: one citation, however many readings
    const groups = new Map();
    for (const f of findings) {
      const key = `${f.reading.kind}|${f.direction}`;
      if (!groups.has(key)) groups.set(key, { kind: f.reading.kind, direction: f.direction, range: f.range, items: [] });
      groups.get(key).items.push(f);
    }
    const ordered = [...groups.values()].sort((a, b) => KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind));

    const list = el('ul', 'ars-vitals__groups');
    for (const g of ordered) {
      g.items.sort((a, b) => b.reading.measured_at_ms - a.reading.measured_at_ms);
      const unit = unitFor(g.kind, (payload.series[g.kind] || {}).unit);
      const li = el('li', 'ars-vitals__group');
      const gh = el('div', 'ars-vitals__group-head');
      gh.append(el('span', 'ars-vitals__group-kind', { text: kindLabel(g.kind) }));
      gh.append(el('span', 'ars-vitals__group-what', {
        text: t(`vitals.outside.${g.direction === 'below' ? 'below' : 'above'}`, getLang(), {
          n: g.items.length, low: num(g.range.low), high: num(g.range.high), unit,
        }),
      }));
      li.append(gh);

      // The citation, verbatim. This is the whole permission this panel has.
      const cite = el('p', 'ars-vitals__cite', {
        text: t('vitals.outside.source', getLang(), { source: g.range.source }),
      });
      if (g.range.note) cite.append(el('span', 'ars-vitals__cite-note', { text: g.range.note }));
      li.append(cite);

      const rows = el('ul', 'ars-vitals__rows');
      const open = expandedGroups.has(`${g.kind}|${g.direction}`);
      const shown = open ? g.items : g.items.slice(0, 2);
      for (const f of shown) rows.append(readingRow(f.reading, { unit, finding: f, mark: false }));
      li.append(rows);

      if (g.items.length > 2) {
        const more = el('button', 'ars-vitals__more', {
          type: 'button',
          text: open ? t('vitals.outside.less', getLang()) : t('vitals.outside.more', getLang(), { n: g.items.length }),
        });
        more.addEventListener('click', () => {
          const key = `${g.kind}|${g.direction}`;
          if (expandedGroups.has(key)) expandedGroups.delete(key); else expandedGroups.add(key);
          renderOutside();
        });
        li.append(more);
      }
      list.append(li);
    }
    body.append(list);
    outsideMount.append(sec);
  }

  const TREND_ARROW = { rising: '↗', falling: '↘', stable: '→', unknown: '·' };

  function renderCards() {
    cards.textContent = '';
    const series = payload.series || {};
    const present = KINDS.filter((k) => series[k] && (series[k].readings || []).length);
    // a kind the protocol has not heard of would still be worth showing
    for (const k of Object.keys(series)) if (!present.includes(k) && (series[k].readings || []).length) present.push(k);

    emptyNote.hidden = present.length > 0;
    cards.hidden = present.length === 0;
    if (!present.length) return;

    // A range is only known for a kind that produced at least one finding — the
    // readings endpoint does not report the range otherwise. See the report.
    const rangeByKind = new Map();
    const outsideById = new Map();
    for (const f of payload.outside_range || []) {
      rangeByKind.set(f.reading.kind, f.range);
      outsideById.set(f.reading.id, f);
    }

    const pending = [];
    for (const kind of present) {
      const entry = series[kind];
      const agg = entry.aggregate || {};
      const unit = unitFor(kind, entry.unit);
      const sorted = [...entry.readings].sort((a, b) => a.measured_at_ms - b.measured_at_ms);
      const latest = sorted[sorted.length - 1];
      const latestOutside = outsideById.get(latest.id);

      const card = el('article', 'ars-vitals__card', { 'aria-label': kindLabel(kind) });
      if (latestOutside) card.dataset.latestOutside = '1';

      const head = el('div', 'ars-vitals__card-head');
      head.append(el('h3', 'ars-vitals__card-kind', { text: kindLabel(kind) }));
      const trend = el('span', 'ars-vitals__trend', { title: t('vitals.trend.explain', getLang()) });
      trend.append(el('span', 'ars-vitals__trend-arrow', { text: TREND_ARROW[agg.trend] || '·', 'aria-hidden': 'true' }));
      trend.append(el('span', null, { text: t(`vitals.trend.${agg.trend || 'unknown'}`, getLang()) }));
      head.append(trend);
      card.append(head);

      const big = el('div', 'ars-vitals__big');
      big.append(el('span', 'ars-vitals__value', { text: num(latest.value) }));
      big.append(el('span', 'ars-vitals__unit', { text: unit }));
      big.append(el('span', 'ars-vitals__when', { text: fmtWhen(latest.measured_at_ms) }));
      card.append(big);

      const { node: chart } = buildChart(kind, entry, rangeByKind.get(kind) || null, new Set(outsideById.keys()));
      card.append(chart);

      const foot = el('div', 'ars-vitals__foot');
      foot.append(el('span', null, {
        text: entry.readings.length === 1
          ? t('vitals.stat.count_one', getLang())
          : t('vitals.stat.count', getLang(), { n: entry.readings.length }),
      }));
      if (entry.readings.length > 1) {
        if (agg.mean !== null && agg.mean !== undefined) {
          foot.append(el('span', null, { text: t('vitals.stat.mean', getLang(), { v: num(agg.mean) }) }));
        }
        if (agg.minimum !== null && agg.minimum !== undefined) {
          foot.append(el('span', null, { text: t('vitals.stat.span', getLang(), { min: num(agg.minimum), max: num(agg.maximum) }) }));
        }
      }
      const open = expandedRows.has(kind);
      const toggle = el('button', 'ars-vitals__toggle', {
        type: 'button', 'aria-expanded': String(open),
        text: open
          ? t('vitals.rows.hide', getLang())
          : (entry.readings.length === 1
            ? t('vitals.rows.show_one', getLang())
            : t('vitals.rows.show', getLang(), { n: entry.readings.length })),
      });
      toggle.addEventListener('click', () => {
        if (expandedRows.has(kind)) expandedRows.delete(kind); else expandedRows.add(kind);
        renderCards();
      });
      foot.append(toggle);
      card.append(foot);

      if (open) {
        const box = el('div', 'ars-vitals__card-rows');
        const rows = el('ul', 'ars-vitals__rows');
        for (const r of [...sorted].reverse()) {
          rows.append(readingRow(r, { unit, finding: outsideById.get(r.id) || null }));
        }
        box.append(rows);
        card.append(box);
      }

      // clicking a point in the chart opens the list and flashes that row
      chart.addEventListener('click', (ev) => {
        const id = ev.target && ev.target.dataset && ev.target.dataset.id;
        if (!id) return;
        expandedRows.add(kind);
        renderCards();
        const row = cards.querySelector(`.ars-vitals__row-item[data-id="${id}"]`);
        if (row) {
          row.dataset.flash = '1';
          row.scrollIntoView({ block: 'nearest' });
          setTimeout(() => row.removeAttribute('data-flash'), 1600);
        }
      });

      cards.append(card);

      if (allowDraw) pending.push(chart);
    }

    // getTotalLength() forces layout. Doing it inside the loop made the browser
    // lay out the panel once per card (5x ~1.3ms); doing it once after every card
    // is attached costs one layout for the lot. Measured: 6.4ms -> 2.1ms on the
    // first render, and it is the only reflow this panel ever forces.
    let delay = 0;
    for (const chart of pending) {
      const path = chart.querySelector('.ars-vitals__line');
      if (!path || !path.dataset.needsLength) continue;
      chart.style.setProperty('--ars-vitals-len', String(Math.ceil(path.getTotalLength())));
      chart.style.setProperty('--ars-vitals-delay', `${delay}ms`);
      chart.dataset.draw = '1';
      delay += 70;
    }
  }

  function renderBlocked() {
    blockedMount.textContent = '';
    blockedMount.hidden = !block;
    if (!block) return;
    const denied = block.status === 403;
    const box = el('section', 'ars-vitals__blocked', { 'data-status': String(block.status), role: 'note' });
    const head = el('div', 'ars-vitals__blocked-head');
    head.append(lockIcon());
    head.append(el('h3', 'ars-vitals__blocked-title', {
      text: t(denied ? 'vitals.blocked.title_denied' : 'vitals.blocked.title', getLang()),
    }));
    box.append(head);
    box.append(el('p', 'ars-vitals__blocked-lead', {
      text: t(denied ? 'vitals.blocked.lead_denied' : 'vitals.blocked.lead', getLang()),
    }));
    if (block.detail) {
      box.append(el('p', 'ars-vitals__blocked-said', { text: t('vitals.blocked.said', getLang()) }));
      box.append(el('blockquote', 'ars-vitals__blocked-quote', { text: block.detail }));
    }
    const actions = el('div', 'ars-vitals__blocked-actions');
    const tab = t('deck.tab.grants', getLang());
    actions.append(el('span', 'ars-vitals__blocked-where', { text: t('vitals.blocked.where', getLang(), { tab }) }));
    if (onOpenAccess) {
      const open = el('button', 'ars-vitals__submit ars-vitals__blocked-open', { type: 'button', text: t('vitals.blocked.open', getLang(), { tab }) });
      open.addEventListener('click', () => onOpenAccess());
      actions.append(open);
    }
    const retry = el('button', 'ars-vitals__more', { type: 'button', text: t('vitals.blocked.retry', getLang()) });
    retry.addEventListener('click', () => refresh({ animate: true }));
    actions.append(retry);
    box.append(actions);
    blockedMount.append(box);
  }

  function renderSummary() {
    const series = payload.series || {};
    const readings = Object.values(series).reduce((n, s) => n + (s.readings || []).length, 0);
    summary.textContent = t('vitals.summary', getLang(), { readings, kinds: Object.keys(series).length });
  }

  function renderAll() {
    renderBlocked();
    // When the guard has refused, there is nothing to summarise and nothing to
    // chart, and an "0 READINGS" line beside a lock would be a second, wrong
    // answer to the same question.
    bar.hidden = !!block;
    if (block) {
      outsideMount.hidden = true;
      cards.hidden = true;
      emptyNote.hidden = true;
      return;
    }
    renderSummary();
    renderOutside();
    renderCards();
  }

  /* ------------------------------------------------------------------ add form */

  const form = el('form', 'ars-vitals__add', { id: 'vitals-add' });
  const addSec = el('section', 'ars-vitals__sec');
  const addHead = el('div', 'ars-vitals__sec-head');
  const addTitle = el('h3', 'ars-vitals__sec-title ars-vitals__sec-title--add', { text: t('vitals.add.title', getLang()) });
  addHead.append(addTitle);
  const addBody = el('div', 'ars-vitals__sec-body');
  addSec.append(addHead, addBody);
  addBody.append(form);
  addMount.append(addSec);

  const addHint = el('p', 'ars-vitals__add-hint', { text: t('vitals.add.hint', getLang()) });
  const kindField = el('label', 'ars-vitals__field');
  const kindLabelEl = el('span', 'ars-vitals__field-label', { text: t('vitals.add.kind', getLang()) });
  const kindSelect = el('select', null, { name: 'kind' });
  for (const k of KINDS) kindSelect.append(el('option', null, { value: k, text: kindLabel(k) }));
  kindField.append(kindLabelEl, kindSelect);

  const row2 = el('div', 'ars-vitals__row-2');
  const valField = el('label', 'ars-vitals__field');
  const valLabelEl = el('span', 'ars-vitals__field-label', { text: t('vitals.add.value', getLang()) });
  const valWrap = el('div', 'ars-vitals__value-wrap');
  const valInput = el('input', null, { type: 'text', inputmode: 'decimal', name: 'value', autocomplete: 'off' });
  const valUnit = el('span', 'ars-vitals__value-unit');
  valWrap.append(valInput, valUnit);
  valField.append(valLabelEl, valWrap);
  const submit = el('button', 'ars-vitals__submit', { type: 'submit', text: t('vitals.add.submit', getLang()) });
  row2.append(valField, submit);

  const noteField = el('label', 'ars-vitals__field');
  const noteLabelEl = el('span', 'ars-vitals__field-label', { text: t('vitals.add.note', getLang()) });
  const noteInput = el('input', null, { type: 'text', name: 'note', autocomplete: 'off', placeholder: t('vitals.add.note_ph', getLang()) });
  noteField.append(noteLabelEl, noteInput);

  form.append(addHint, kindField, row2, noteField);

  function syncUnit() {
    const k = kindSelect.value;
    valUnit.textContent = unitFor(k, (payload.series[k] || {}).unit);
  }
  kindSelect.addEventListener('change', syncUnit);
  syncUnit();

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const raw = valInput.value.trim().replace(',', '.');
    const value = Number(raw);
    if (!raw || !Number.isFinite(value)) { say(t('vitals.add.need_value', getLang()), 'warn'); valInput.focus(); return; }
    const kind = kindSelect.value;
    const note = noteInput.value.trim();
    submit.disabled = true;
    const original = submit.textContent;
    submit.textContent = t('vitals.add.saving', getLang());
    try {
      const res = await fetch(`${baseUrl}/api/health/readings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, value, note: note || undefined }),
      });
      if (res.status === 428 || res.status === 403) {
        const denied = res.status === 403;
        say(t(denied ? 'vitals.blocked.write_denied' : 'vitals.blocked.write', getLang()), denied ? 'error' : 'warn');
        const detail = await guardDetail(res);
        if (detail) say(detail, 'warn');   // the guard's sentence, verbatim
        if (onOpenAccess) say(t('vitals.blocked.where', getLang(), { tab: t('deck.tab.grants', getLang()) }), 'warn');
        return;
      }
      if (!res.ok) {
        const reason = (await guardDetail(res)) || `HTTP ${res.status}`;
        say(t('vitals.add.refused', getLang(), { reason }), 'error');
        return;
      }
      const body = await res.json();
      const unit = unitFor(kind, null);
      const finding = (body.outside_range || [])[0];
      if (finding) {
        say(t('vitals.add.saved_outside', getLang(), { value: num(value), unit, source: finding.range.source }), 'warn');
      } else {
        say(t('vitals.add.saved', getLang(), { value: num(value), unit }));
      }
      valInput.value = '';
      noteInput.value = '';
      await refresh();
      valInput.focus();
    } catch (err) {
      console.warn('[vitals] record failed', err);
      say(t('vitals.add.failed', getLang()), 'error');
    } finally {
      submit.disabled = false;
      submit.textContent = original;
    }
  });

  /* ---------------------------------------------------------------- network */

  /** -> { ok } | { ok: false, status, detail } so the caller can tell "the gateway
   *  is down" from "the guard will not allow it", which need different sentences. */
  async function destroyReading(id) {
    try {
      const res = await fetch(`${baseUrl}/api/health/readings/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (res.status === 428 || res.status === 403) {
        return { ok: false, status: res.status, detail: await guardDetail(res) };
      }
      if (!res.ok) return { ok: false };
      const body = await res.json();
      // The endpoint answers {removed: <rows deleted>} today; a boolean would be a
      // reasonable thing for it to answer tomorrow. Zero rows means the reading is
      // still in the store, and the user must be told that rather than shown a row
      // vanishing from a list while the record survives on disk.
      const removed = body && body.removed;
      if (typeof removed === 'number') return { ok: removed > 0 };
      return { ok: removed !== false };
    } catch (err) {
      console.warn('[vitals] delete failed', err);
      return { ok: false };
    }
  }

  let inflight = false;
  let loadedOnce = false;
  let loadedOnceRendered = false;
  let lastRenderMs = 0;
  async function refresh({ animate = false } = {}) {
    if (inflight || offline) return;
    inflight = true;
    lastFetchAt = Date.now();
    try {
      const res = await fetch(`${baseUrl}/api/health/readings?days=${days}`);
      if (res.status === 428 || res.status === 403) {
        // The guard fronts this endpoint. Refused is not empty: drop whatever was
        // on screen (it is health data the user has not authorised showing) and
        // say which of the two it is, in the guard's own words.
        block = { status: res.status, detail: await guardDetail(res) };
        payload = { days, series: {}, outside_range: [] };
        loadedOnce = true;
        renderAll();
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      block = null;
      payload = await res.json();
      loadedOnce = true;
      allowDraw = animate || !loadedOnceRendered;
      loadedOnceRendered = true;
      const t0 = performance.now();
      renderAll();
      lastRenderMs = performance.now() - t0;
      allowDraw = false;
      syncUnit();
    } catch (err) {
      console.warn('[vitals] refresh failed', err);
      if (!loadedOnce) say(t('vitals.unreachable', getLang()), 'error');
    } finally {
      inflight = false;
    }
  }

  // NO TIMER POLL. Every read of this record is a guarded call — HEALTH_READ is
  // HIGH, and the guard rate-limits it (4 per turn, 64 per session) precisely to
  // catch a client that reads private data in a loop. A background poll IS that
  // loop: a 60s timer would spend the whole session budget on requests nobody
  // asked for and then hand the user a 403 that reads like a refusal of them.
  //
  // So a read happens only when a person caused one: opening the tab, changing the
  // window, pressing "check again", or after their own write or delete. Coming back
  // to the tab re-reads only if what is on screen has gone stale.
  const STALE_MS = 60000;
  let lastFetchAt = 0;

  let onScreen = false;
  const io = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
      const now = entries.some((e) => e.isIntersecting);
      const appeared = now && !onScreen;
      onScreen = now;
      // Blocked always retries on re-entry: the user has just been sent to the
      // Access tab to make a grant, and coming back is them saying "now try again".
      if (appeared && (block || Date.now() - lastFetchAt > STALE_MS)) refresh({ animate: true });
    }, { threshold: 0 })
    : null;
  if (io) io.observe(wrap);

  function onVisibility() {
    if (!document.hidden && onScreen && (block || Date.now() - lastFetchAt > STALE_MS)) refresh();
  }
  document.addEventListener('visibilitychange', onVisibility);

  refresh({ animate: true });

  const api = {
    panelRoot: panelHandle.root,
    refresh,
    setOffline(v) { offline = !!v; },
    /** For the deck badge: how many readings sit outside a cited range. */
    outsideCount: () => (block ? 0 : (payload.outside_range || []).length),
    blocked: () => (block ? { ...block } : null),
    stats: () => ({ renderMs: Math.round(lastRenderMs * 100) / 100, days, kinds: Object.keys(payload.series || {}).length, blocked: block && block.status }),
    retranslate() {
      if (panelHandle.setTitle) panelHandle.setTitle(t('vitals.title', getLang()));
      intro.textContent = t('vitals.intro', getLang());
      emptyNote.textContent = t('vitals.empty', getLang());
      windows.setAttribute('aria-label', t('vitals.window.label', getLang()));
      for (const [d, b] of windowBtns) {
        b.textContent = t(`vitals.window.${d}.short`, getLang());
        b.title = t(`vitals.window.${d}`, getLang());
      }
      addTitle.textContent = t('vitals.add.title', getLang());
      addHint.textContent = t('vitals.add.hint', getLang());
      kindLabelEl.textContent = t('vitals.add.kind', getLang());
      valLabelEl.textContent = t('vitals.add.value', getLang());
      noteLabelEl.textContent = t('vitals.add.note', getLang());
      noteInput.placeholder = t('vitals.add.note_ph', getLang());
      submit.textContent = t('vitals.add.submit', getLang());
      [...kindSelect.options].forEach((o) => { o.textContent = kindLabel(o.value); });
      numFmt.clear(); dayFmt.clear(); timeFmt.clear();
      syncUnit();
      renderAll();
    },
    destroy() {
      if (io) io.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      panelHandle.root.remove();
    },
  };
  // Same hook brain.js exposes: lets a driver measure this panel from the page
  // without the shell having to hand it around.
  panelHandle.root.__vitals = api;
  return api;
}

export { KINDS };
