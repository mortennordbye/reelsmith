import React from "react";
import { AbsoluteFill, Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";

import type { Shot, SpecArtefact } from "../episodeSchema";
import { font, paper } from "./theme";

/**
 * The pieces an episode is cut from. Written here rather than imported from the
 * prototypes, because the prototypes are episodes and this is machinery.
 */

/**
 * One artefact, cropped to the frame by the spec rather than by this component.
 *
 * The crop is carried, never measured. Measuring an image inside a frame render
 * is how one frame ends up different from its neighbour for no visible reason,
 * which is the same rule `pageAspect` exists for on the reel side.
 *
 * The drift is deliberately almost nothing. A scan that pans is a scan being
 * shown; a scan that swoops is a motion graphic, which is what every generated
 * channel does and what this format is trying not to look like.
 */
export const Scan: React.FC<{ artefact: SpecArtefact; crop: Shot["crop"]; fit?: string }> = ({
  artefact,
  crop,
  fit = "cover",
}) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const rect = crop ?? { sx: 0, sy: 0, sw: artefact.w, sh: artefact.h };

  // Scale so the cropped rectangle exactly fills the frame, then offset so the
  // rectangle's top left lands at the frame's top left.
  // "contain" shows the whole artefact on the ground rather than a detail of
  // it, which is what the opening shot of a landscape scan wants.
  const scale =
    fit === "contain"
      ? Math.min(width / artefact.w, height / artefact.h)
      : Math.max(width / rect.sw, height / rect.sh);
  const drift = interpolate(frame, [0, 120], [1, 1.02], { extrapolateRight: "clamp" });

  return (
    <AbsoluteFill style={{ overflow: "hidden", background: paper.ground }}>
      <Img
        src={staticFile(artefact.src)}
        style={{
          position: "absolute",
          width: artefact.w * scale,
          height: artefact.h * scale,
          left: fit === "contain" ? (width - artefact.w * scale) / 2 : -rect.sx * scale,
          // A contained artefact sits high rather than centred. Centring it
          // put an empty band above and below in equal measure, and the band
          // below is where the words go, so the one above was the third of a
          // vertical frame the reel side already learned not to waste.
          top: fit === "contain" ? height * 0.14 : -rect.sy * scale,
          transform: `scale(${drift})`,
          transformOrigin: "center",
        }}
      />
    </AbsoluteFill>
  );
};

/** Film grain, which breaks up the flatness a scaled scan otherwise has. */
export const Grain: React.FC = () => (
  <AbsoluteFill style={{ opacity: 0.06, mixBlendMode: "multiply", pointerEvents: "none" }}>
    <svg width="100%" height="100%">
      <filter id="episode-grain">
        <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="3" />
      </filter>
      <rect width="100%" height="100%" filter="url(#episode-grain)" />
    </svg>
  </AbsoluteFill>
);

/**
 * The band the words sit in.
 *
 * A scrim rather than a solid block, and it stops short of the bottom edge,
 * because the reel's own rule is that painting the bottom out leaves a dead
 * strip between the last readable thing and the words.
 */
export const Scrim: React.FC<{ height: number }> = ({ height }) => (
  <AbsoluteFill
    style={{
      top: `${100 - height}%`,
      background: `linear-gradient(to bottom, rgba(242,238,229,0), ${paper.ground} 38%)`,
    }}
  />
);

/**
 * A line of the script, set by which beat of the arc it belongs to.
 *
 * Nothing that has to be read sits in the top of the frame. Instagram puts the
 * back arrow and the account name there, TikTok its Following tabs, and a
 * notched phone loses more again.
 */
export const Line: React.FC<{ shot: Shot }> = ({ shot }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = spring({ frame, fps, config: { damping: 200 }, durationInFrames: 12 });
  const quote = shot.kind === "quote";

  return (
    <AbsoluteFill>
      <Scrim height={quote ? 62 : 46} />
      <AbsoluteFill
        style={{
          justifyContent: "flex-end",
          padding: "0 84px 300px",
          opacity: rise,
          transform: `translateY(${interpolate(rise, [0, 1], [16, 0])}px)`,
        }}
      >
        {shot.step ? (
          <div style={{ display: "flex", alignItems: "baseline", gap: 28 }}>
            <span
              style={{
                fontFamily: font.mono,
                fontSize: 64,
                color: paper.verdigris,
                fontWeight: 700,
              }}
            >
              {shot.step}
            </span>
            <span style={{ fontFamily: font.display, fontSize: 84, fontWeight: 800, lineHeight: 1.06, color: paper.ink }}>
              {shot.line}
            </span>
          </div>
        ) : (
          <span
            style={{
              fontFamily: quote ? font.source : font.display,
              fontSize: quote ? 78 : 88,
              fontWeight: quote ? 500 : 800,
              fontStyle: quote ? "italic" : "normal",
              lineHeight: 1.1,
              letterSpacing: quote ? 0 : -2,
              color: quote ? paper.verdigris : paper.ink,
            }}
          >
            {shot.line}
          </span>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/** The hook, on screen for the three seconds skip rate scores. */
export const Hook: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = spring({ frame, fps, config: { damping: 200 }, durationInFrames: 14 });
  return (
    <AbsoluteFill>
      {/* The hook had none in the first render and sat straight on a
          seventeenth century map, where black type on engraved hatching is
          unreadable at the one moment the format cannot afford to be. */}
      <Scrim height={52} />
      <AbsoluteFill style={{ justifyContent: "flex-end", padding: "0 84px 300px", opacity: rise }}>
      <span
        style={{
          fontFamily: font.display,
          fontSize: 104,
          fontWeight: 900,
          lineHeight: 1.02,
          letterSpacing: -4,
          color: paper.ink,
        }}
      >
        {text}
      </span>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/**
 * The citation, which is the whole differentiator and therefore always on
 * screen rather than only in the caption. Small, in the safe band, and set in
 * mono so it reads as a reference rather than as part of the sentence.
 */
export const Source: React.FC<{ text: string }> = ({ text }) => (
  <AbsoluteFill style={{ justifyContent: "flex-end", padding: "0 84px 150px" }}>
    <span
      style={{
        fontFamily: font.mono,
        fontSize: 30,
        letterSpacing: 1,
        color: paper.inkDim,
        textTransform: "uppercase",
      }}
    >
      {text}
    </span>
  </AbsoluteFill>
);

/** The sign off. Empty fields mean no end card rather than an empty one. */
export const EndCard: React.FC<{ name: string; handle: string; tagline: string }> = ({
  name,
  handle,
  tagline,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = spring({ frame, fps, config: { damping: 200 }, durationInFrames: 16 });
  if (!name && !handle && !tagline) return null;
  return (
    <AbsoluteFill style={{ background: paper.ground }}>
      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          textAlign: "center",
          opacity: rise,
        }}
      >
        <span style={{ fontFamily: font.display, fontSize: 104, fontWeight: 900, letterSpacing: -4, color: paper.ink }}>
          {name}
        </span>
        <span style={{ fontFamily: font.mono, fontSize: 34, letterSpacing: 2, color: paper.verdigris, marginTop: 30 }}>
          {handle}
        </span>
        <span style={{ fontFamily: font.source, fontSize: 54, color: paper.inkDim, marginTop: 40 }}>
          {tagline}
        </span>
      </AbsoluteFill>
      <Grain />
    </AbsoluteFill>
  );
};
