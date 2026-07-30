import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";

import { theme } from "../theme";

/**
 * Deliberately NOT a grid.
 *
 * The faint tech-grid overlay is one of the most recognisable generated-video
 * tells -- it shows up in every AI explainer template, so it reads as
 * "template" before the viewer has processed a single word. Same for
 * particle fields and circuit-board lines.
 *
 * What replaces it: a soft off-centre light wash, a vignette to pull the eye
 * to the middle, and fine film grain. Grain in particular does a lot of work
 * -- it breaks up the mathematically flat gradients that make CG backgrounds
 * look synthetic, and costs nothing to render.
 */

// Static SVG noise as a data URI. Generated once by the browser and cached,
// rather than recomputed per frame.
const GRAIN = `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='220' height='220'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='220' height='220' filter='url(%23n)'/%3E%3C/svg%3E")`;

export const Background: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const progress = durationInFrames > 0 ? frame / durationInFrames : 0;

  // One slow pass across the frame over the whole video -- slow enough that
  // you never catch it moving, fast enough that no two seconds look identical.
  const washX = 62 + Math.sin(progress * Math.PI) * 16;
  const washY = 26 + Math.cos(progress * Math.PI * 0.7) * 10;

  return (
    <AbsoluteFill style={{ backgroundColor: theme.color.bgDeep }}>
      {/* Primary cool wash, off-centre so the composition isn't symmetrical */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse 80% 55% at ${washX}% ${washY}%, ${theme.color.accent}3A 0%, transparent 64%)`,
        }}
      />
      {/* Warm counterpoint low and opposite, so the frame isn't monochrome */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse 70% 45% at ${100 - washX}% ${92 - washY / 2}%, ${theme.color.accentWarm}16 0%, transparent 58%)`,
        }}
      />
      {/* Vignette: darkens the edges so captions and cards read as foreground.
          Kept light -- stacked with the hook scrim it crushes the opening
          screenshot, and the frame is already near-black to begin with. */}
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse 95% 75% at 50% 45%, transparent 38%, rgba(1,4,9,0.5) 100%)",
        }}
      />
      {/* Film grain */}
      <AbsoluteFill
        style={{
          backgroundImage: GRAIN,
          backgroundRepeat: "repeat",
          opacity: 0.055,
          mixBlendMode: "overlay",
        }}
      />
    </AbsoluteFill>
  );
};
