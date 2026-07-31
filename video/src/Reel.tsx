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

// There was a watermark here carrying the repo name for the whole video. It is
// gone on purpose. The name now appears exactly twice: in the opening README
// shot, where it is part of the page being shown, and in the voiceover. Every
// other repetition weakened the only reason to comment, which is that the
// caption will send you the link.
//
// If attribution ever matters more than conversion, bring it back with the
// account handle rather than the repo name. A repost is worth marking; a repo
// name is the thing being traded.

// How long the end card holds. Long enough to read and act on, short enough
// that it does not eat the last point the voiceover is making.
const CTA_SECONDS = 4;

/**
 * The ask, on screen. The caption carries the same line, but a caption sits
 * behind a "more" tap that most viewers never make, so without this the whole
 * comment-to-DM mechanic depends on an interaction that does not happen.
 *
 * Deliberately plain: no arrows, no emoji, no "link in bio". The instruction is
 * the entire message and anything else competes with it.
 */
const CallToAction: React.FC<{ keyword: string }> = ({ keyword }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  // Rises rather than pops. A spring here reads as a template transition.
  const enter = interpolate(frame, [0, 0.4 * fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: 420,
        opacity: enter,
      }}
    >
      <div
        style={{
          transform: `translateY(${(1 - enter) * 24}px)`,
          textAlign: "center",
          padding: "28px 44px",
          borderRadius: 28,
          backgroundColor: "rgba(1, 4, 9, 0.82)",
          border: `2px solid ${theme.color.accent}`,
        }}
      >
        <div
          style={{
            fontFamily: theme.font.display,
            fontSize: 62,
            fontWeight: 800,
            color: theme.color.text,
            lineHeight: 1.1,
          }}
        >
          Comment <span style={{ color: theme.color.accent }}>{keyword}</span>
        </div>
        <div
          style={{
            fontFamily: theme.font.display,
            fontSize: 34,
            fontWeight: 600,
            color: theme.color.muted,
            marginTop: 10,
          }}
        >
          and I send you the link
        </div>
      </div>
    </AbsoluteFill>
  );
};

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

      {/* Anchored to the end rather than given a fixed slot, so it always lands
          under the closing line of the voiceover however long the script ran. */}
      {spec.ctaKeyword ? (
        <Sequence
          from={Math.max(0, spec.durationInFrames - CTA_SECONDS * fps)}
          durationInFrames={CTA_SECONDS * fps}
          layout="none"
        >
          <CallToAction keyword={spec.ctaKeyword} />
        </Sequence>
      ) : null}

      {spec.audioSrc ? <Audio src={staticFile(spec.audioSrc)} /> : null}
    </AbsoluteFill>
  );
};
