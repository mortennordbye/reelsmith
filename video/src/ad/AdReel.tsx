import React from "react";
import {
  AbsoluteFill,
  Audio,
  Easing,
  Img,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

import { theme } from "../theme";
import type { Caption, CueItem, Scene, VideoSpec } from "../types";

/**
 * The ad format: one device per beat of the script, set like a considered
 * product film rather than a slide deck. Built on 2026-09-26 from three hand
 * made prototypes (AutoResearch, MarkItDown, Spec Kit) and generalised so the
 * scriptwriter picks the device and this file only draws it.
 *
 * What it is built from, and why each piece is here:
 *
 * - **The README is the opening and a recurring shot.** The hero is the one
 *   thing on screen nobody could generate, so the video opens on it, and a
 *   `readme` scene shows the README's own block for the line the voice is on.
 * - **Captions light word by word from the real timings**, and the scene's
 *   emphasis words settle into the accent as they are said. The whole line is
 *   readable ahead of the voice; brightness is what moves.
 * - **The info scenes are set like print, not like a dashboard.** Flat ground,
 *   hairline rules, a type scale doing the work, colour only as small marks.
 *   Glass cards, glow and pills were tried in the prototypes and read as
 *   generated at a glance.
 * - **Paper scenes cut in hard.** A statement or a verdict cuts to an off
 *   white frame and back, which is the rhythm product films use between
 *   feature shots and statement frames. Everything else zooms through.
 *
 * Nothing here measures anything at render time. Positions come from the spec
 * or from fixed layout, and text sizes are computed from character counts, so
 * a frame never depends on a layout read that could differ from its neighbour.
 */

const font = theme.font;

const c = {
  ground: "#05080f",
  ink: "#f4f7fb",
  inkDim: "#8b98ab",
  inkFaint: "#566276",
  accent: "#5aa2ff",
  bad: "#ff6b6b",
};
const paper = { ground: "#eeece7", ink: "#0d0d0f", inkDim: "#6d6a63", rule: "rgba(0,0,0,0.16)", bad: "#d4402f", accent: "#1f5fbf" };
const hair = "rgba(255,255,255,0.16)";
const noLig: React.CSSProperties = { fontVariantLigatures: "none", fontFeatureSettings: '"liga" 0, "calt" 0' };

const OVERLAP = 10;
/** Scenes set on paper. They cut in and out hard, take no vignette, and dark captions. */
const PAPER = new Set(["statement", "verdict"]);
/** Scenes whose device already shows the words, so the caption stays off. */
const NO_CAPTION = new Set(["statement"]);

/**
 * Whether a caption line is what a statement scene already has on screen.
 * Only then is the caption redundant; a statement that paraphrases the line
 * keeps it, or the viewer gets five seconds of voice with no words.
 */
const saysTitle = (scene: Scene, line: { words: { t: string }[] }) => {
  const title = new Set((scene.title ?? "").split(/\s+/).map(norm).filter(Boolean));
  if (!title.size) return false;
  const spoken = line.words.map((w) => norm(w.t));
  const shared = spoken.filter((w) => title.has(w)).length;
  return shared >= Math.ceil(title.size * 0.6) && spoken.length <= title.size + 3;
};
/** Scenes whose device sits high, so the caption goes to the bottom band. */
const CAPTION_BOTTOM = new Set(["verdict", "command", "terminal", "readme", "screenshot"]);

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const tween = (f: number, from: number, len: number) => interpolate(f, [from, from + Math.max(1, len)], [0, 1], { ...clamp, easing: ease });

const hexA = (hex: string, a: number) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};
const mix = (a: string, b: string, t: number) => {
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const ch = (sh: number) => Math.round(((pa >> sh) & 255) * (1 - t) + ((pb >> sh) & 255) * t);
  return `#${[ch(16), ch(8), ch(0)].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
};
const norm = (t: string) => t.toLowerCase().replace(/[^a-z0-9]/g, "");

/**
 * The largest font size at which the longest run of text fits the width.
 * Average glyph widths rather than measurement, so it is the same every frame;
 * the factors are Inter Black and JetBrains Mono, rounded up to stay inside.
 */
const fit = (longest: number, max: number, min: number, width = 920, glyph = 0.62) =>
  Math.max(min, Math.min(max, Math.floor(width / (Math.max(1, longest) * glyph))));

/* ------------------------------------------------------------- timing */

type Word = { t: string; at: number };

/** Every spoken word as a frame, from the Whisper timings. */
const wordsOf = (captions: Caption[], fps: number): Word[] =>
  captions.map((w) => ({ t: w.text, at: Math.round((w.startMs / 1000) * fps) }));

/**
 * The frame at which a scene's voice says a word, relative to the scene, or
 * null. Matched on the word's start so "PowerPoint," finds "powerpoint".
 */
const saidIn = (words: Word[], scene: Scene, word: string): number | null => {
  const key = norm(word.split(/\s+/)[0] ?? "");
  if (!key) return null;
  const end = scene.fromFrame + scene.durationInFrames;
  const hit = words.find((w) => w.at >= scene.fromFrame - 3 && w.at < end && norm(w.t).startsWith(key));
  return hit ? hit.at - scene.fromFrame : null;
};

type Line = { start: number; end: number; words: Word[] };

/**
 * The captions as lines: a sentence, or a long sentence split at a comma past
 * fourteen words, so no caption runs past what its band can hold.
 */
const linesOf = (words: Word[], last: number): Line[] => {
  const out: Line[] = [];
  let cur: Word[] = [];
  const flush = () => {
    if (cur.length) out.push({ start: cur[0].at, end: 0, words: cur });
    cur = [];
  };
  for (const w of words) {
    cur.push(w);
    if (/[.?!]$/.test(w.t) || (cur.length >= 14 && /,$/.test(w.t)) || cur.length >= 22) flush();
  }
  flush();
  out.forEach((l, i) => (l.end = i + 1 < out.length ? out[i + 1].start : last));
  return out;
};

/* ------------------------------------------------------------ ground */

const Lights: React.FC = () => {
  const f = useCurrentFrame();
  const p = (s: number, ph: number, amp: number, base: number) => base + amp * Math.sin(f / s + ph);
  return (
    <AbsoluteFill
      style={{
        background: [
          `radial-gradient(circle at ${p(70, 0, 18, 25)}% ${p(90, 1, 10, 22)}%, #17213a 0%, transparent 48%)`,
          `radial-gradient(circle at ${p(80, 2, 16, 78)}% ${p(60, 3, 12, 55)}%, #101826 0%, transparent 46%)`,
          `radial-gradient(circle at ${p(65, 4, 20, 40)}% ${p(75, 5, 10, 88)}%, #161a24 0%, transparent 50%)`,
          c.ground,
        ].join(","),
      }}
    />
  );
};

const Vignette: React.FC = () => (
  <AbsoluteFill style={{ background: "radial-gradient(ellipse 85% 75% at 50% 48%, transparent 55%, rgba(0,0,0,0.6))" }} />
);

/** Film grain, so flat panels and gradients do not read as CG. */
const Grain: React.FC = () => (
  <AbsoluteFill style={{ opacity: 0.07, mixBlendMode: "overlay", pointerEvents: "none" }}>
    <svg width="100%" height="100%">
      <filter id="ad-grain">
        <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="3" />
      </filter>
      <rect width="100%" height="100%" filter="url(#ad-grain)" />
    </svg>
  </AbsoluteFill>
);

/* ------------------------------------------------------------ camera */

/**
 * A scene arrives as a camera pulling back through it and leaves by pushing on
 * into the next, over the frames where the two overlap.
 */
const Shell: React.FC<{ dur: number; children: React.ReactNode; cut?: boolean; last?: boolean }> = ({ dur, children, cut, last }) => {
  const frame = useCurrentFrame();
  if (cut) return <AbsoluteFill>{children}</AbsoluteFill>;
  const inT = tween(frame, 0, 16);
  const outT = last ? 0 : interpolate(frame, [dur - OVERLAP, dur], [0, 1], { ...clamp, easing: Easing.in(Easing.cubic) });
  const scale = interpolate(inT, [0, 1], [1.12, 1]) * interpolate(outT, [0, 1], [1, 1.14]);
  const blur = (1 - inT) * 18 + outT * 22;
  return (
    <AbsoluteFill style={{ opacity: inT * (1 - outT), transform: `scale(${scale})`, filter: blur > 0.3 ? `blur(${blur}px)` : undefined }}>
      {children}
    </AbsoluteFill>
  );
};

/** The object of a scene, tilted into place and then drifting a few degrees. */
const Tilt: React.FC<{ dur: number; children: React.ReactNode; from?: { x: number; y: number }; drift?: number }> = ({
  dur,
  children,
  from = { x: 8, y: -6 },
  drift = 3,
}) => {
  const frame = useCurrentFrame();
  const t = tween(frame, 0, 26);
  const rx = interpolate(t, [0, 1], [from.x, 2]) + Math.sin(frame / 40) * 0.6;
  const ry = interpolate(t, [0, 1], [from.y, 0]) + interpolate(frame, [0, Math.max(1, dur)], [-drift / 2, drift / 2]);
  return <AbsoluteFill style={{ transform: `perspective(2000px) rotateX(${rx}deg) rotateY(${ry}deg)` }}>{children}</AbsoluteFill>;
};

/** A band of light crossing a surface once, as it lands. */
const Sheen: React.FC<{ at?: number; radius?: number }> = ({ at = 14, radius = 20 }) => {
  const frame = useCurrentFrame();
  if (frame < at || frame > at + 30) return null;
  const x = interpolate(frame, [at, at + 30], [140, -40], { ...clamp, easing: Easing.inOut(Easing.cubic) });
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        borderRadius: radius,
        pointerEvents: "none",
        background: "linear-gradient(110deg, transparent 38%, rgba(255,255,255,0.08) 47%, rgba(255,255,255,0.16) 50%, rgba(255,255,255,0.08) 53%, transparent 62%)",
        backgroundSize: "260% 100%",
        backgroundPosition: `${x}% 0`,
        mixBlendMode: "screen",
      }}
    />
  );
};

/* ------------------------------------------------------------ pieces */

const Rule: React.FC<{ at: number; color?: string; style?: React.CSSProperties }> = ({ at, color = hair, style }) => {
  const frame = useCurrentFrame();
  return <div style={{ height: 1, background: color, transform: `scaleX(${tween(frame, at, 22)})`, transformOrigin: "left", ...style }} />;
};

const Label: React.FC<{ children: React.ReactNode; color?: string; style?: React.CSSProperties }> = ({ children, color = c.inkDim, style }) => (
  <span style={{ fontFamily: font.mono, fontSize: 22, letterSpacing: 3.5, textTransform: "uppercase", color, ...style }}>{children}</span>
);

/** A mark drawn in one stroke, like a pen. */
const Mark: React.FC<{ ok: boolean; at: number; color: string; size: number }> = ({ ok, at, color, size }) => {
  const frame = useCurrentFrame();
  const t = interpolate(frame, [at, at + 12], [0, 1], { ...clamp, easing: Easing.out(Easing.cubic) });
  const t2 = interpolate(frame, [at + 8, at + 18], [0, 1], { ...clamp, easing: Easing.out(Easing.cubic) });
  const L = 200;
  const stroke = { fill: "none", stroke: color, strokeWidth: 8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, strokeDasharray: L };
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" style={{ display: "block" }}>
      {ok ? (
        <path d="M18 54 L42 76 L84 26" {...stroke} strokeDashoffset={L * (1 - t)} />
      ) : (
        <>
          <path d="M24 24 L76 76" {...stroke} strokeDashoffset={L * (1 - t)} />
          <path d="M76 24 L24 76" {...stroke} strokeDashoffset={L * (1 - t2)} />
        </>
      )}
    </svg>
  );
};

/* ------------------------------------------------------------ captions */

/**
 * Each word rises out of a mask as the line arrives, then the voice lights it.
 * Emphasis words settle into the accent with a rule drawn under them.
 */
/**
 * Which words of a line belong to an emphasis phrase. Phrases, not single
 * words: the scriptwriter returns "96 gigabytes" or "entirely on your
 * machine", and lighting every "your" in the line would be noise.
 */
const emphasised = (words: Word[], phrases: string[]): Set<number> => {
  const toks = words.map((w) => norm(w.t));
  const out = new Set<number>();
  for (const p of phrases) {
    const want = p.split(/\s+/).map(norm).filter(Boolean);
    if (!want.length) continue;
    for (let i = 0; i + want.length <= toks.length; i++) {
      if (want.every((t, k) => toks[i + k] === t)) want.forEach((_, k) => out.add(i + k));
    }
  }
  return out;
};

const Spoken: React.FC<{ words: Word[]; size: number; weight: number; keys: Set<number>; ink: string; accent: string }> = ({
  words,
  size,
  weight,
  keys,
  ink,
  accent,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const pad = size * 0.16;
  return (
    <span style={{ fontFamily: font.display, fontSize: size, fontWeight: weight, lineHeight: 1.06, letterSpacing: -size * 0.04 }}>
      {words.map((w, i) => {
        const key = keys.has(i);
        const rise = tween(frame, i * 1.4, 16);
        const on = spring({ frame: frame - w.at, fps, config: { damping: 200 }, durationInFrames: 6 });
        const warm = key ? tween(frame, w.at + 3, 12) : 0;
        const rule = key ? tween(frame, w.at + 6, 14) : 0;
        const nextKey = keys.has(i + 1);
        return (
          <React.Fragment key={i}>
            <span style={{ display: "inline-block", clipPath: "inset(-50% -60% 0 -60%)", verticalAlign: "top", paddingBottom: pad, marginBottom: -pad }}>
              <span
                style={{
                  position: "relative",
                  display: "inline-block",
                  transform: `translateY(${(1 - rise) * 110}%) rotate(${(1 - rise) * 4}deg)`,
                  transformOrigin: "0 100%",
                  color: warm > 0 ? mix(ink, accent, warm) : ink,
                  opacity: interpolate(on, [0, 1], [0.3, 1]),
                }}
              >
                {w.t}
                {key ? (
                  <span
                    style={{
                      position: "absolute",
                      left: 0,
                      right: nextKey ? -size * 0.26 : 0,
                      bottom: -size * 0.04,
                      height: Math.max(6, size * 0.06),
                      background: accent,
                      transform: `scaleX(${rule})`,
                      transformOrigin: "left",
                      borderRadius: 3,
                    }}
                  />
                ) : null}
              </span>
            </span>{" "}
          </React.Fragment>
        );
      })}
    </span>
  );
};

const CaptionLine: React.FC<{ words: Word[]; dur: number; bottom: boolean; paperGround: boolean; phrases: string[]; hook?: boolean }> = ({
  words,
  dur,
  bottom,
  paperGround,
  phrases,
  hook,
}) => {
  const keys = emphasised(words, phrases);
  const frame = useCurrentFrame();
  const n = words.length;
  const size = hook ? 90 : n <= 3 ? 112 : n <= 8 ? 84 : n <= 14 ? 72 : 62;
  const out = tween(frame, dur - 5, 5);
  const bar = tween(frame, 2, 18);
  const accent = paperGround ? paper.accent : c.accent;
  return (
    <AbsoluteFill
      style={{
        justifyContent: bottom ? "flex-end" : "flex-start",
        // 300 at the top is safeTop; 300 at the bottom clears the platform's caption band.
        padding: bottom ? "0 80px 300px" : "300px 80px 0",
        opacity: 1 - out,
        transform: `translateY(${out * (bottom ? 20 : -20)}px)`,
      }}
    >
      <div style={{ display: "flex", gap: 32 }}>
        <div style={{ width: 4, background: accent, transform: `scaleY(${bar})`, transformOrigin: bottom ? "bottom" : "top", flexShrink: 0 }} />
        <Spoken words={words} size={size} weight={hook ? 900 : 800} keys={keys} ink={paperGround ? paper.ink : c.ink} accent={accent} />
      </div>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------ scenes */

type SceneProps = { scene: Scene; words: Word[]; spec: VideoSpec; hero: string | null };

/** When the voice says an item's label in this scene, or a stagger if it never does. */
const cueAt = (words: Word[], scene: Scene, text: string, i: number, base = 8, step = 14) => saidIn(words, scene, text) ?? base + i * step;

/** The maintainer's own README hero, lifted off the page, the repo's address under it. */
const HeroScene: React.FC<SceneProps> = ({ scene, spec, hero }) => {
  const frame = useCurrentFrame();
  const src = scene.imageSrc ?? hero;
  const push = interpolate(frame, [0, scene.durationInFrames], [1, 1.05], { easing: Easing.out(Easing.quad) });
  if (!src) return <StatementScene scene={{ ...scene, title: spec.repo.name }} words={[]} spec={spec} hero={hero} />;
  return (
    <AbsoluteFill>
      <Tilt dur={scene.durationInFrames} from={{ x: 18, y: -10 }} drift={4}>
        <div
          style={{
            position: "absolute",
            left: 90,
            top: 300,
            width: 900,
            maxHeight: 820,
            borderRadius: 22,
            overflow: "hidden",
            boxShadow: "0 60px 140px rgba(0,0,0,0.65), 0 0 0 1px rgba(255,255,255,0.08)",
            transform: `scale(${push})`,
            transformOrigin: "50% 0",
          }}
        >
          <Img src={staticFile(src)} style={{ width: "100%", display: "block" }} />
          <Sheen at={16} radius={22} />
        </div>
      </Tilt>
      <AbsoluteFill style={{ justifyContent: "flex-end", padding: "0 80px 150px", opacity: tween(frame, 14, 14) }}>
        <span style={{ fontFamily: font.mono, fontSize: 30, color: c.inkDim }}>{spec.repo.url.replace(/^https?:\/\//, "")}</span>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/** One short line, set as large as the frame allows on paper, each word landing as it is said. */
const StatementScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const text = (scene.title ?? "").trim();
  const parts = text.split(/\s+/);
  // Words land on the voice only when the voice opens with them. A title that
  // paraphrases the sentence would otherwise wait on words said late or never,
  // and the first real render held an empty sheet of paper for two seconds.
  const opensWith = (saidIn(words, scene, parts[0]) ?? 99) <= 12;
  const longest = Math.max(...parts.map((p) => p.length));
  const size = Math.min(text.length <= 12 ? 200 : text.length <= 20 ? 160 : 124, fit(longest, 220, 80, 940, 0.6));
  let last = 0;
  return (
    <AbsoluteFill style={{ background: paper.ground, justifyContent: "center", padding: "0 70px" }}>
      {scene.subtitle ? (
        <div style={{ fontFamily: font.mono, fontSize: 28, letterSpacing: 3, color: paper.inkDim, marginBottom: 18, textTransform: "uppercase" }}>{scene.subtitle}</div>
      ) : null}
      <div style={{ fontFamily: font.display, fontWeight: 900, fontSize: size, letterSpacing: -size * 0.05, lineHeight: 0.98, color: paper.ink }}>
        {parts.map((p, i) => {
          const heard = opensWith ? saidIn(words, scene, p) : null;
          const at = heard !== null && heard >= last ? heard : last + (i ? 4 : 2);
          last = at;
          return (
            <span key={i} style={{ display: "inline-block", marginRight: size * 0.22, opacity: frame >= at ? 1 : 0, transform: `scale(${interpolate(frame, [at, at + 8], [1.05, 1], clamp)})`, transformOrigin: "left center" }}>
              {p}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** Two to four words stacked huge, each slamming in as the voice names it. */
const PosterScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const list = scene.bullets.slice(0, 4);
  const size = fit(Math.max(...list.map((w) => w.length)), 200, 90, 920, 0.66);
  const keys = new Set(scene.emphasis.flatMap((p) => p.split(/\s+/)).map(norm));
  const rowH = size * 1.02;
  const top = Math.max(720, 1580 - rowH * list.length);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 60, top }}>
        {scene.title ? <Label style={{ display: "block", marginBottom: 20 }}>{scene.title}</Label> : null}
        {list.map((w, i) => {
          const at = cueAt(words, scene, w, i, 6, 12);
          const hit = tween(frame, at, 9);
          const lit = keys.has(norm(w)) ? tween(frame, at + 6, 12) : 0;
          return (
            <div key={i} style={{ clipPath: "inset(-10% -10% 0 -10%)", height: rowH }}>
              <div
                style={{
                  fontFamily: font.display,
                  fontWeight: 900,
                  fontSize: size,
                  lineHeight: 0.98,
                  letterSpacing: -size * 0.03,
                  color: mix(c.ink, c.accent, lit),
                  opacity: hit,
                  transform: `translateX(${(1 - hit) * -110}px)`,
                  textTransform: "uppercase",
                }}
              >
                {w}
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** On paper: each item's mark drawn as it is said. Columns for two, rows for three. */
const VerdictScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const items = scene.items.slice(0, 3);
  const cols = items.length <= 2;
  const longest = Math.max(...items.map((i) => Math.max(...i.label.split(/\s+/).map((w) => w.length))));
  const labelSize = cols ? fit(longest, 66, 40, 400, 0.6) : fit(longest, 60, 40, 560, 0.6);
  return (
    <AbsoluteFill style={{ background: paper.ground }}>
      <div style={{ position: "absolute", left: 80, right: 80, top: 340 }}>
        {scene.title ? (
          <>
            <Label color={paper.inkDim}>{scene.title}</Label>
            <Rule at={0} color={paper.rule} style={{ margin: "20px 0 36px" }} />
          </>
        ) : null}
        <div style={{ display: "flex", flexDirection: cols ? "row" : "column" }}>
          {items.map((it, i) => {
            const at = cueAt(words, scene, it.label, i, 6, 18);
            const on = tween(frame, at - 2, 14);
            const col = it.ok ? paper.ink : paper.bad;
            return (
              <div
                key={i}
                style={{
                  flex: 1,
                  display: "flex",
                  flexDirection: cols ? "column" : "row",
                  alignItems: cols ? "flex-start" : "center",
                  gap: cols ? 0 : 30,
                  paddingLeft: cols && i ? 34 : 0,
                  paddingRight: cols && !i ? 24 : 0,
                  borderLeft: cols && i ? `1px solid ${paper.rule}` : undefined,
                  borderTop: !cols && i ? `1px solid ${paper.rule}` : undefined,
                  padding: cols ? undefined : "26px 0",
                  opacity: on,
                }}
              >
                {cols ? <div style={{ fontFamily: font.mono, fontSize: 22, color: paper.inkDim }}>0{i + 1}</div> : null}
                <div style={{ order: cols ? 0 : 2, flex: cols ? undefined : 1 }}>
                  <div style={{ fontFamily: font.display, fontSize: labelSize, fontWeight: 800, letterSpacing: -2.5, lineHeight: 1.02, color: paper.ink, marginTop: cols ? 10 : 0 }}>{it.label}</div>
                  {!cols && it.note ? <div style={{ fontFamily: font.display, fontSize: 28, fontWeight: 500, color: col, marginTop: 8 }}>{it.note}</div> : null}
                </div>
                <div style={{ margin: cols ? "26px 0 12px -12px" : 0, order: 1 }}>
                  <Mark ok={!!it.ok} at={at + 8} color={col} size={cols ? 210 : 130} />
                </div>
                {cols && it.note ? <div style={{ order: 2, fontFamily: font.display, fontSize: 30, fontWeight: 500, color: col }}>{it.note}</div> : null}
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};

/** A ledger of rows; the winner takes the accent as the voice names it. */
const CompareScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const items = scene.items.length ? scene.items : scene.bullets.map((b) => ({ label: b, note: "", ok: null, value: null }) as CueItem);
  const winner = items.findIndex((i) => i.ok);
  const winAt = winner >= 0 ? cueAt(words, scene, items[winner].label, winner, 20, 0) : 0;
  const rowH = items.length > 3 ? 128 : 150;
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 80, top: 800 }}>
        <div style={{ paddingBottom: 18, opacity: tween(frame, 0, 12) }}>
          <Label>{scene.title ?? ""}</Label>
        </div>
        <Rule at={0} />
        {items.slice(0, 4).map((r, i) => {
          const on = tween(frame, 3 + i * 5, 14);
          const lit = i === winner ? tween(frame, winAt, 14) : 0;
          return (
            <div key={i}>
              <div style={{ display: "flex", alignItems: "center", height: rowH, gap: 30, opacity: on, transform: `translateY(${(1 - on) * 18}px)` }}>
                <span style={{ flex: 1, fontFamily: font.display, fontSize: fit(r.label.length, 60, 36, 520, 0.56), fontWeight: i === winner ? 700 : 500, letterSpacing: -2, color: mix(c.ink, c.accent, lit), lineHeight: 1.02 }}>
                  {r.label}
                </span>
                <span style={{ fontFamily: font.display, fontSize: 30, color: i === winner ? mix(c.inkDim, c.accent, lit) : c.inkDim, textAlign: "right", maxWidth: 440 }}>{r.note}</span>
              </div>
              <Rule at={5 + i * 5} />
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** Values drawn to scale, each bar growing as its label is said. */
const BarsScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const items = scene.items.slice(0, 4);
  const max = Math.max(...items.map((i) => i.value ?? 0), 1);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 80, top: 800 }}>
        <Label>{scene.title ?? ""}</Label>
        <Rule at={0} style={{ margin: "16px 0 10px" }} />
        {items.map((r, i) => {
          const at = cueAt(words, scene, r.label, i, 8, 12);
          const grow = tween(frame, at, 28);
          const big = (r.value ?? 0) === max;
          return (
            <div key={i} style={{ marginTop: 34 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <span style={{ fontFamily: font.display, fontSize: 34, fontWeight: 500, color: c.ink }}>{r.label}</span>
                <span style={{ fontFamily: font.mono, fontSize: 30, color: big ? c.ink : c.inkDim, opacity: grow }}>{r.note}</span>
              </div>
              <div style={{ height: 38, marginTop: 14, background: "#171b22" }}>
                <div style={{ height: "100%", width: `${((r.value ?? 0) / max) * 100 * grow}%`, background: big ? c.accent : "#5b6370" }} />
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** Break a command into lines that fit the poster, keeping a flag with its argument. */
const commandLines = (code: string, perLine: number) => {
  const tokens = code.trim().split(/\s+/).reduce<string[]>((acc, w) => {
    const last = acc[acc.length - 1];
    if (last && /^-/.test(last) && !last.includes(" ")) acc[acc.length - 1] = `${last} ${w}`;
    else acc.push(w);
    return acc;
  }, []);
  const lines: string[] = [];
  for (const t of tokens) {
    const cur = lines[lines.length - 1];
    if (cur !== undefined && (cur + " " + t).length <= perLine) lines[lines.length - 1] = `${cur} ${t}`;
    else lines.push(t);
  }
  return lines;
};

/** The command as a poster in the terminal's own face, typing, with its facts under it. */
const CommandScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const code = (scene.code ?? "").split("\n")[0];
  // Sized so the longest unbreakable piece fits, not the whole line: a URL or
  // an env assignment is one token, and sizing from the line put
  // ANTHROPIC_BASE_URL="http://..." a thousand pixels past the frame.
  const longestToken = Math.max(...code.split(/\s+/).map((t) => t.length), 1);
  const size = fit(Math.max(longestToken, Math.min(code.length, 18)), 78, 30, 880, 0.6);
  const perLine = Math.floor(880 / (size * 0.6)) - 2;
  const lines = commandLines(code, perLine);
  const total = lines.join("").length;
  const typed = Math.floor(interpolate(frame, [2, 28], [0, total], clamp));
  let left = typed;
  const caret = Math.floor(frame / 8) % 2 === 0 || typed < total;
  const factsTop = 360 + lines.length * size * 1.18 + 70;
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 60, top: 360, ...noLig }}>
        {lines.map((p, i) => {
          const shown = p.slice(0, Math.max(0, left));
          const done = left >= p.length;
          left -= p.length;
          const current = (!done && shown.length > 0) || (i === lines.length - 1 && done);
          return (
            <div key={i} style={{ fontFamily: font.mono, fontSize: size, fontWeight: 700, color: c.ink, lineHeight: 1.18, letterSpacing: -size * 0.02, wordBreak: "break-all", minHeight: size * 1.18 }}>
              <span style={{ color: i === 0 ? c.inkFaint : "transparent" }}>$ </span>
              {shown}
              {current ? (
                <span style={{ display: "inline-block", width: size * 0.55, height: size, marginLeft: 6, verticalAlign: `-${size * 0.16}px`, background: caret ? c.accent : "transparent" }} />
              ) : null}
            </div>
          );
        })}
      </div>
      {scene.items.length ? (
        <div style={{ position: "absolute", left: 80, right: 80, top: factsTop }}>
          <Rule at={20} />
          {scene.items.slice(0, 3).map((it, i) => {
            const at = cueAt(words, scene, it.label, i, 26, 10);
            return (
              <div key={i}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", height: 96, paddingTop: 30, opacity: tween(frame, at - 2, 14) }}>
                  <span style={{ fontFamily: font.display, fontSize: 40, fontWeight: 500, color: c.ink }}>{it.label}</span>
                  <span style={{ fontFamily: font.mono, fontSize: 34, color: c.accent, ...noLig }}>{it.note}</span>
                </div>
                <Rule at={at} />
              </div>
            );
          })}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

/** A block of code or a file tree as a poster, lines appearing in turn. */
const CodeScene: React.FC<SceneProps> = ({ scene }) => {
  const frame = useCurrentFrame();
  const lines = (scene.code ?? "").split("\n").slice(0, 12);
  const size = fit(Math.max(...lines.map((l) => l.length)), 44, 22, 920, 0.6);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 60, top: 760, ...noLig }}>
        {scene.title ? <Label style={{ display: "block", marginBottom: 20 }}>{scene.title}</Label> : null}
        <Rule at={0} style={{ marginBottom: 24 }} />
        {lines.map((l, i) => (
          <div key={i} style={{ fontFamily: font.mono, fontSize: size, lineHeight: 1.6, color: c.ink, whiteSpace: "pre", opacity: tween(frame, 4 + i * 3, 10) }}>
            {l || " "}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};

/** What the tool writes, as divider cards dropped in one after another. */
const FilesScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const items = scene.items.slice(0, 4);
  const tabW = 920 / Math.max(4, items.length);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 80, top: 820 }}>
        {items.map((it, i) => {
          const at = cueAt(words, scene, it.note || it.label, i, 6, 12);
          const d = spring({ frame: frame - at, fps, config: { damping: 16, mass: 0.8 }, durationInFrames: 22 });
          const shade = ["#1a1c21", "#1f2227", "#25282e", "#2b2f36"][i];
          return (
            <div
              key={i}
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                top: 46 + i * 118,
                height: 220,
                background: shade,
                borderRadius: "0 12px 12px 12px",
                boxShadow: "0 -10px 30px rgba(0,0,0,0.35)",
                opacity: Math.min(1, d * 1.4),
                transform: `translateY(${(1 - d) * -70}px)`,
              }}
            >
              <div
                style={{
                  position: "absolute",
                  top: -44,
                  left: i * tabW,
                  width: tabW - 8,
                  height: 44,
                  background: shade,
                  borderRadius: "10px 10px 0 0",
                  display: "flex",
                  alignItems: "center",
                  padding: "0 14px",
                  fontFamily: font.mono,
                  fontSize: fit(it.label.length, 22, 15, tabW - 30, 0.6),
                  color: c.ink,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  ...noLig,
                }}
              >
                {it.label}
              </div>
              <div style={{ padding: "26px 28px", fontFamily: font.display, fontSize: 30, color: c.inkDim }}>{it.note}</div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/**
 * The README's own block, captured by the pipeline. Its lines light as the
 * voice names something in them, or in order when the voice names none.
 */
const ReadmeScene: React.FC<SceneProps> = (props) => {
  const { scene, words, spec } = props;
  const frame = useCurrentFrame();
  const sec = scene.section;
  if (!sec) return <HeroScene {...props} />;
  const winW = 920;
  const scale = winW / sec.w;
  const winH = Math.min(sec.h * scale, 760);
  const spokenHere = words.filter((w) => w.at >= scene.fromFrame && w.at < scene.fromFrame + scene.durationInFrames);
  const lit = sec.lines.map((l) => {
    const toks = new Set(l.text.split(/[^A-Za-z0-9]+/).map(norm).filter((t) => t.length >= 3 && !STOP.has(t)));
    const hit = spokenHere.find((w) => toks.has(norm(w.t)));
    return hit ? hit.at - scene.fromFrame : null;
  });
  const anyHeard = lit.some((x) => x !== null);
  const at = lit.map((x, i) => (anyHeard ? x : 10 + i * 6));
  const first = Math.min(...at.filter((x): x is number => x !== null), 9999);
  const dim = tween(frame, first - 4, 12);
  const appear = tween(frame, 0, 18);
  const top = scene.kind === "readme" ? 300 : 720;
  return (
    <AbsoluteFill>
      <div
        style={{
          position: "absolute",
          left: 80,
          top,
          width: winW,
          height: winH + 56,
          borderRadius: 14,
          overflow: "hidden",
          background: "#0d1117",
          outline: `1px solid ${hair}`,
          boxShadow: "0 40px 100px rgba(0,0,0,0.6)",
          opacity: appear,
          transform: `translateY(${(1 - appear) * 40}px)`,
        }}
      >
        <div style={{ height: 56, display: "flex", alignItems: "center", gap: 14, padding: "0 24px", borderBottom: `1px solid ${hair}`, fontFamily: font.mono, fontSize: 22, color: c.inkDim }}>
          <span>{spec.repo.fullName}</span>
          <span style={{ color: c.inkFaint }}>README.md</span>
        </div>
        <div style={{ position: "relative", height: winH, overflow: "hidden" }}>
          <Img src={staticFile(sec.src)} style={{ position: "absolute", left: 0, top: 0, width: sec.w * scale, height: sec.h * scale }} />
          <div style={{ position: "absolute", inset: 0, background: "#0d1117", opacity: 0.55 * dim }} />
          {sec.lines.map((l, i) => {
            const a = at[i];
            if (a === null) return null;
            const on = tween(frame, a, 8);
            return (
              <div key={i} style={{ position: "absolute", left: 0, right: 0, top: l.y * scale, height: l.h * scale, overflow: "hidden", opacity: on, borderLeft: `4px solid ${c.accent}` }}>
                <Img src={staticFile(sec.src)} style={{ position: "absolute", width: sec.w * scale, height: sec.h * scale, left: -4, top: -l.y * scale }} />
                <div style={{ position: "absolute", inset: 0, background: hexA(c.accent, 0.12) }} />
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};

/**
 * Words too common to say a line was named. Without this "and" lit "... and
 * more!" on the first render, because the voice says "and" in every sentence.
 */
const STOP = new Set(
  "the and for with you your from that this into are was not but its all any can one out use has have what when will more than then them they their there which about after before over only also just".split(" "),
);

/** Parse "0.6s", "2,577", "170k" into a number to count up to, and the text around it. */
const parseStat = (v: string) => {
  const m = v.match(/^([^\d]*)([\d,]*\.?\d+)(.*)$/);
  if (!m) return null;
  const n = parseFloat(m[2].replace(/,/g, ""));
  if (!isFinite(n)) return null;
  return { pre: m[1], n, post: m[3], decimals: (m[2].split(".")[1] ?? "").length, commas: m[2].includes(",") };
};

/** One real figure, set large and thin, counting up to itself. */
const StatScene: React.FC<SceneProps> = ({ scene }) => {
  const frame = useCurrentFrame();
  const value = scene.statValue ?? "";
  const p = parseStat(value);
  const k = tween(frame, 4, 30);
  const shown = p
    ? `${p.pre}${(p.n * k).toLocaleString("en-US", { minimumFractionDigits: p.decimals, maximumFractionDigits: p.decimals, useGrouping: p.commas })}${p.post}`
    : value;
  const size = fit(value.length, 300, 110, 920, 0.55);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 80, top: 820 }}>
        <div style={{ fontFamily: font.display, fontSize: size, fontWeight: 200, letterSpacing: -size * 0.05, lineHeight: 1, color: c.ink }}>{shown}</div>
        <Rule at={10} style={{ margin: "30px 0 22px" }} />
        <div style={{ fontFamily: font.display, fontSize: 36, color: c.inkDim, opacity: tween(frame, 12, 14) }}>{scene.statLabel}</div>
      </div>
    </AbsoluteFill>
  );
};

/** A project's own named stages on a line, filling in order. */
const DiagramScene: React.FC<SceneProps> = ({ scene, words }) => {
  const frame = useCurrentFrame();
  const nodes = scene.diagramNodes ?? [];
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", left: 80, right: 80, top: 800 }}>
        {nodes.map((n, i) => {
          const at = cueAt(words, scene, n, i, 6, 10);
          const on = tween(frame, at, 12);
          return (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 28, height: 118, opacity: 0.25 + 0.75 * on }}>
              <span style={{ fontFamily: font.mono, fontSize: 22, color: c.inkFaint, width: 40 }}>0{i + 1}</span>
              <div style={{ width: 18, height: 18, borderRadius: 9, background: on > 0.5 ? c.accent : "#2a2d33" }} />
              <span style={{ fontFamily: font.display, fontSize: fit(n.length, 58, 34, 760, 0.56), fontWeight: 600, letterSpacing: -1.5, color: c.ink }}>{n}</span>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

const SCENES: Record<string, React.FC<SceneProps>> = {
  screenshot: HeroScene,
  statement: StatementScene,
  poster: PosterScene,
  verdict: VerdictScene,
  compare: CompareScene,
  bullets: CompareScene,
  bars: BarsScene,
  command: CommandScene,
  terminal: CommandScene,
  code: CodeScene,
  files: FilesScene,
  readme: ReadmeScene,
  stat: StatScene,
  diagram: DiagramScene,
  repo_card: HeroScene,
};

/* ------------------------------------------------------------ end card */

const count = (n: number) => (n >= 10000 ? `${Math.round(n / 1000)}k` : n.toLocaleString("en-US"));

/** The repo's own title block, set large, its numbers under a rule, then the ask. */
const EndCard: React.FC<{ spec: VideoSpec }> = ({ spec }) => {
  const frame = useCurrentFrame();
  const r = spec.repo;
  const up = (at: number) => ({ opacity: tween(frame, at, 14), transform: `translateY(${(1 - tween(frame, at, 18)) * 24}px)` });
  const stats: [string, string][] = [["Stars", count(r.stars)]];
  if (r.starsGainedToday) stats.push(["Today", `+${r.starsGainedToday.toLocaleString("en-US")}`]);
  if (r.language) stats.push(["Language", r.language]);
  if (r.license) stats.push(["Licence", r.license]);
  const nameSize = fit(r.name.length, 132, 70, 920, 0.58);
  return (
    <AbsoluteFill>
      <Lights />
      <div style={{ position: "absolute", left: 80, right: 80, top: 440 }}>
        <div style={up(0)}>
          <Label>{r.owner}</Label>
        </div>
        <div style={{ ...up(4), fontFamily: font.display, fontSize: nameSize, fontWeight: 700, letterSpacing: -nameSize * 0.045, color: c.ink, lineHeight: 1.02, marginTop: 14 }}>{r.name}</div>
        <Rule at={14} style={{ marginTop: 56 }} />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", rowGap: 34, marginTop: 36 }}>
          {stats.slice(0, 4).map(([label, value], i) => (
            <div key={label} style={up(18 + i * 4)}>
              <Label>{label}</Label>
              <div style={{ fontFamily: font.display, fontSize: 56, fontWeight: 300, letterSpacing: -1.5, color: c.ink, marginTop: 8 }}>{value}</div>
            </div>
          ))}
        </div>
        <Rule at={30} style={{ marginTop: 40 }} />
      </div>
      <AbsoluteFill style={{ justifyContent: "flex-end", padding: "0 80px 330px" }}>
        <div style={up(34)}>
          <span style={{ fontFamily: font.display, fontSize: 46, fontWeight: 500, letterSpacing: -1, color: c.ink }}>
            Follow{spec.endcardHandle ? " " : ""}
            <span style={{ color: c.accent }}>{spec.endcardHandle}</span>
          </span>
          {spec.endcardTagline ? <div style={{ fontFamily: font.display, fontSize: 32, color: c.inkDim, marginTop: 10 }}>{spec.endcardTagline}</div> : null}
        </div>
      </AbsoluteFill>
      <Vignette />
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------ reel */

export const AdReel: React.FC<VideoSpec> = (spec) => {
  const { fps, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const words = wordsOf(spec.captions, fps);
  const hero = spec.scenes.find((s) => s.kind === "screenshot" && s.imageSrc)?.imageSrc ?? null;
  // The end card takes over where the ask starts. Without a recorded ask it
  // holds the last two seconds, so the video still signs off.
  const endAt = spec.ctaFromFrame ?? Math.max(0, durationInFrames - 60);
  const scenes = spec.scenes.filter((s) => s.fromFrame < endAt);
  const intro = scenes[0]?.fromFrame === 0 && scenes[0].kind === "screenshot" ? scenes[0].fromFrame + scenes[0].durationInFrames : 0;
  const lines = linesOf(words, endAt).filter((l) => l.start >= intro && l.start < endAt);
  const sceneAt = (f: number) => scenes.find((s) => f >= s.fromFrame && f < s.fromFrame + s.durationInFrames) ?? scenes[scenes.length - 1];
  const onPaper = scenes.some((s) => PAPER.has(s.kind) && frame >= s.fromFrame && frame < s.fromFrame + s.durationInFrames);
  const hookWords = spec.hook.split(/\s+/).map((t, i) => ({ t, at: 8 + i * 3 }));

  return (
    <AbsoluteFill style={{ background: c.ground }}>
      {spec.audioSrc ? <Audio src={staticFile(spec.audioSrc)} /> : null}
      <Lights />
      {scenes.map((s, i) => {
        const Scene = SCENES[s.kind] ?? CompareScene;
        const cut = PAPER.has(s.kind);
        const dur = Math.min(s.durationInFrames, endAt - s.fromFrame);
        const tail = cut || i === scenes.length - 1 ? 0 : OVERLAP;
        return (
          <Sequence key={i} from={s.fromFrame} durationInFrames={dur + tail}>
            <Shell dur={dur + tail} cut={cut}>
              <Scene scene={s} words={words} spec={spec} hero={hero} />
            </Shell>
          </Sequence>
        );
      })}
      {onPaper ? null : <Vignette />}
      {intro > 0 ? (
        <Sequence from={0} durationInFrames={intro}>
          <CaptionLine words={hookWords} dur={intro} bottom paperGround={false} phrases={spec.hookEmphasis} hook />
        </Sequence>
      ) : null}
      {lines.map((l, i) => {
        // A few frames in, so a line that starts on a cut belongs to the scene it opens.
        const s = sceneAt(l.start + 3);
        if (!s || (NO_CAPTION.has(s.kind) && saysTitle(s, l))) return null;
        return (
          <Sequence key={i} from={l.start} durationInFrames={Math.max(1, l.end - l.start)}>
            <CaptionLine
              words={l.words.map((w) => ({ t: w.t, at: w.at - l.start }))}
              dur={l.end - l.start}
              bottom={CAPTION_BOTTOM.has(s.kind)}
              paperGround={PAPER.has(s.kind)}
              phrases={s.emphasis}
            />
          </Sequence>
        );
      })}
      <Sequence from={endAt}>
        <Shell dur={durationInFrames - endAt} last>
          <EndCard spec={spec} />
        </Shell>
      </Sequence>
      <Grain />
    </AbsoluteFill>
  );
};
