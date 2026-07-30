import React from "react";
import {
  AbsoluteFill,
  Audio,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

import { Background } from "./components/Background";
import { Captions } from "./components/Captions";
import { SceneRenderer } from "./scenes/SceneRenderer";
import { theme } from "./theme";
import type { VideoSpec } from "./types";

/** The hook overlay owns the first 3 seconds -- the only part most viewers see. */
const HOOK_SECONDS = 3;

const Hook: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const durationInFrames = HOOK_SECONDS * fps;

  const enter = spring({ frame, fps, config: { damping: 200, stiffness: 180, mass: 0.6 } });
  // Fade out over the last 12 frames rather than cutting, which reads as a
  // glitch at this size.
  const exit = interpolate(
    frame,
    [durationInFrames - 12, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        paddingLeft: theme.padding,
        paddingRight: theme.padding,
        paddingBottom: 240,
        // Light scrim + defocus rather than a heavy flat overlay. The point of
        // opening on the real GitHub page is that the viewer recognises it, so
        // burying it under 72% black defeats the exercise. Blurring it instead
        // keeps the hook dominant while the page stays legible behind.
        backgroundColor: "rgba(1,4,9,0.38)",
        backdropFilter: "blur(3px)",
        WebkitBackdropFilter: "blur(3px)",
        opacity: exit,
      }}
    >
      <div
        style={{
          fontFamily: theme.font.display,
          fontSize: theme.size.hook,
          fontWeight: 900,
          color: theme.color.text,
          textAlign: "center",
          lineHeight: 1.08,
          letterSpacing: "-0.04em",
          textShadow: "0 8px 40px rgba(0,0,0,0.9)",
          transform: `scale(${interpolate(enter, [0, 1], [0.88, 1])})`,
        }}
      >
        {text}
      </div>
      <div
        style={{
          marginTop: 44,
          height: 8,
          width: interpolate(enter, [0, 1], [0, 220]),
          borderRadius: 4,
          backgroundColor: theme.color.accent,
        }}
      />
    </AbsoluteFill>
  );
};

const Watermark: React.FC<{ handle: string }> = ({ handle }) => (
  <AbsoluteFill style={{ justifyContent: "flex-start", alignItems: "center", paddingTop: 120 }}>
    <span
      style={{
        fontFamily: theme.font.mono,
        fontSize: 30,
        color: theme.color.muted,
        letterSpacing: "0.14em",
      }}
    >
      {handle}
    </span>
  </AbsoluteFill>
);

export const Reel: React.FC<VideoSpec> = (spec) => {
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: theme.color.bgDeep }}>
      <Background />

      {spec.scenes.map((scene, i) => (
        <Sequence
          key={i}
          from={scene.fromFrame}
          durationInFrames={scene.durationInFrames}
          // Each scene animates from its own frame 0, which is what lets the
          // spring/stagger animations inside restart per scene.
          layout="none"
        >
          <SceneRenderer scene={scene} repo={spec.repo} />
        </Sequence>
      ))}

      <Captions captions={spec.captions} />

      <Sequence durationInFrames={HOOK_SECONDS * fps} layout="none">
        <Hook text={spec.hook} />
      </Sequence>

      <Watermark handle={`github.com/${spec.repo.fullName}`} />

      {spec.audioSrc ? <Audio src={staticFile(spec.audioSrc)} /> : null}
    </AbsoluteFill>
  );
};
