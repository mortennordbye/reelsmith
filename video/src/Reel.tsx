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
/**
 * How far through the video the ask appears, as a fraction of its length.
 *
 * It used to be the last four seconds, which had two costs. Anyone who left
 * before 85% never saw it at all, and because it took the bottom band from the
 * captions the video ended on a card rather than on content, so there was
 * nothing to loop back into. Replays are watch time.
 */
const CTA_FROM_FRACTION = 0.55;

/**
 * The ask, on screen. The caption carries the same line, but a caption sits
 * behind a "more" tap that most viewers never make, so without this the whole
 * comment-to-DM mechanic depends on an interaction that does not happen.
 *
 * Deliberately plain: no arrows, no emoji, no "link in bio". The instruction is
 * the entire message and anything else competes with it.
 *
 * It sits at the TOP of the frame, which is the only strip nothing else uses:
 * scene content starts at y=300 and the captions own the bottom band. That is
 * what lets it run for the back half of the video alongside the captions
 * instead of replacing them for the last four seconds.
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
        justifyContent: "flex-start",
        alignItems: "center",
        paddingTop: 96,
        opacity: enter,
      }}
    >
      <div
        style={{
          transform: `translateY(${(1 - enter) * -18}px)`,
          textAlign: "center",
          padding: "18px 32px",
          borderRadius: 22,
          backgroundColor: "rgba(1, 4, 9, 0.82)",
          border: `2px solid ${theme.color.accent}`,
        }}
      >
        <div
          style={{
            fontFamily: theme.font.display,
            fontSize: 44,
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
            fontSize: 26,
            fontWeight: 600,
            color: theme.color.muted,
            marginTop: 6,
          }}
        >
          if you want the link
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const Reel: React.FC<VideoSpec> = (spec) => {
  const { fps } = useVideoConfig();
  // Where the ask appears. It no longer takes anything from the captions, so
  // they run to the last frame and the video ends on content.
  const ctaFrom = spec.ctaKeyword
    ? Math.round(spec.durationInFrames * CTA_FROM_FRACTION)
    : undefined;

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

      {/* Runs from the middle to the last frame rather than sitting in a slot at
          the end, so a viewer who leaves at 70% has still seen the ask. It is at
          the top of the frame, so the captions keep the bottom band and the
          video's final frame is scene content, which loops cleanly back into
          the hook. */}
      {spec.ctaKeyword && ctaFrom !== undefined ? (
        <Sequence
          from={ctaFrom}
          durationInFrames={spec.durationInFrames - ctaFrom}
          layout="none"
        >
          <CallToAction keyword={spec.ctaKeyword} />
        </Sequence>
      ) : null}

      {spec.audioSrc ? <Audio src={staticFile(spec.audioSrc)} /> : null}
    </AbsoluteFill>
  );
};
