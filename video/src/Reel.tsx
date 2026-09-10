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
import { OpeningSceneContext, SceneRenderer } from "./scenes/SceneRenderer";
import { browserChromeHeight, safeTop, theme } from "./theme";
import type { VideoSpec } from "./types";

/** The hook overlay owns the first 3 seconds -- the only part most viewers see. */
/**
 * How long the hook holds.
 *
 * Trimmed from 3.0. Meta's skip rate counts viewers who scroll past inside the
 * first three seconds, and across the first seven posts this account lost 64 to
 * 80 percent of them there against a 30 to 40 percent average for the format.
 * Whatever happens in this window is the whole contest, and the video used to
 * spend all of it on one static text card.
 */
const HOOK_SECONDS = 2.4;

/** How far below the hook band the scrim fades out, in pixels. */
const HOOK_FADE = 180;

const Hook: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const durationInFrames = HOOK_SECONDS * fps;

  const enter = spring({ frame, fps, config: { damping: 200, stiffness: 180, mass: 0.6 } });
  // The hero resolves while the hook is on it, rather than sitting behind a
  // fixed scrim until the hook leaves.
  //
  // Two things were wrong with holding it flat. The point of opening on the
  // real GitHub page is that the viewer recognises it, and three seconds of
  // 3px blur is three seconds of deliberately preventing that, during the only
  // window where recognition decides anything. And a frame where nothing
  // changes is a frame with no reason to keep watching; the push-in underneath
  // is 0.5 percent over this stretch, which nobody perceives.
  //
  // The scrim thins rather than clearing. It is what keeps the hook legible on
  // a light README hero, and plenty of them are light.
  // Resolves inside the first second rather than over three quarters of the
  // hook, and starts at a blur that still reads as a GitHub page rather than as
  // a smear. Recognition is the entire reason for opening on the hero, and
  // `skip_rate` scores the first three seconds, so anything still resolving at
  // second two resolved after the decision.
  //
  // The scrim is unchanged. It is what keeps white hook text legible on a light
  // README, plenty of them are light, and darkening a sharp screenshot costs
  // recognition far less than blurring it does.
  const resolve = interpolate(frame, [0, fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // Denser than it was, and the strip is what pays for it. When the hook sat
  // centred over the hero, every point of scrim was charged against the one
  // element the shot existed to show, so 0.55 falling to 0.25 was the most it
  // could afford and the comment below is written about that trade. A strip
  // darkens the top quarter and leaves the rest of the page untouched, so the
  // hook can be properly legible over a full-bleed README -- which is a busy,
  // light-on-dark page of real text rather than the small card on empty
  // background this used to sit on.
  //
  // Near opaque, and that is a decision rather than a slider being nudged.
  // A GitHub README is mostly dark and then suddenly is not: the brightest
  // thing on the page is a row of white shields badges, and maintainers put it
  // directly under the title, which is exactly where the strip lands. 0.78 was
  // legible over prose and unreadable over AutoResearch's two award badges, so
  // the floor has to clear the worst case rather than the common one.
  //
  // What it costs is the top ~300px of the page for 2.4 seconds. What it used
  // to cost, when the hook was centred, was the whole hero for the entire
  // window skip rate scores. Recognition is carried by the chrome, which stays
  // sharp, and by the rest of the page, which is now full bleed, scrolling and
  // visible for the remaining twenty four seconds.
  const scrim = interpolate(resolve, [0, 1], [0.97, 0.93]);
  const blur = interpolate(resolve, [0, 1], [2, 0]);
  // Fade out over the last 12 frames rather than cutting, which reads as a
  // glitch at this size.
  const exit = interpolate(
    frame,
    [durationInFrames - 12, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  return (
    <AbsoluteFill style={{ opacity: exit }}>
      {/*
        The hook sits in a band at the top rather than centred on the hero.
        Centred, it covered the project's own wordmark for the entire window
        `skip_rate` scores, and cleared at frame 90 at exactly the moment the
        page suddenly looked good. CLAUDE.md already forbids this composition
        for the cover stills, for the same reason and in stronger words:
        "Nothing may cover it. Centring was tried and it buried the one element
        the cover exists to show." The video had the rule and did not apply it.

        The scrim is a gradient that is dense behind the text and gone by the
        middle of the frame, so the hero is legible underneath the hook rather
        than after it. A flat full-frame scrim is what made the old opening a
        dark rectangle with words on it.
      */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          // Below the platform's chrome and below the browser title bar, so
          // the hook never starts on top of the window it is sitting in.
          paddingTop: safeTop + browserChromeHeight + 36,
          paddingLeft: theme.padding,
          paddingRight: theme.padding,
          paddingBottom: 28,
          // Flat, not a gradient. The gradient version put its stops at fixed
          // percentages of the strip, and the strip's height depends on
          // whether the hook wrapped to two lines or three, so the fade landed
          // in a different place for every hook. On a two line hook it began
          // at 62 percent, which is the second line, and the scrim under that
          // line measured about 0.46 whatever the nominal value was set to.
          // That is how a hook stayed unreadable over AutoResearch's award
          // badges while this number was raised twice.
          //
          // The fade is a separate element below, with a height in pixels, so
          // the text always sits on the full value and the transition is the
          // same for a one line hook and a three line one.
          backgroundColor: `rgba(1,4,9,${scrim})`,
          backdropFilter: `blur(${blur}px)`,
          WebkitBackdropFilter: `blur(${blur}px)`,
        }}
      >
        <div
          style={{
            fontFamily: theme.font.display,
            // Smaller than the centred hook was. A strip has a width budget
            // rather than the whole frame, and 104 wrapped every hook to four
            // lines here.
            fontSize: theme.size.hookStrip,
            fontWeight: 900,
            color: theme.color.text,
            // Left aligned, not centred. "Centred, symmetric layouts
            // throughout" is on this account's own list of generated-video
            // tells, and a strip is the one place the layout can have an
            // opinion without costing anything.
            textAlign: "left",
            lineHeight: 1.08,
            letterSpacing: "-0.04em",
            textShadow: "0 8px 40px rgba(0,0,0,0.9)",
            transform: `translateY(${interpolate(enter, [0, 1], [-18, 0])}px)`,
            opacity: enter,
          }}
        >
          {text}
        </div>
        <div
          style={{
            marginTop: 36,
            height: 8,
            width: interpolate(enter, [0, 1], [0, 220]),
            borderRadius: 4,
            backgroundColor: theme.color.accent,
          }}
        />
        {/* The fade out of the band, in pixels below it rather than as a
            percentage stop inside it. See the note on backgroundColor. */}
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: -HOOK_FADE,
            height: HOOK_FADE,
            background: `linear-gradient(180deg, rgba(1,4,9,${scrim}) 0%, rgba(1,4,9,0) 100%)`,
          }}
        />
      </div>
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
 *
 * Then it was 0.55, on the theory that earlier is seen by more people. The
 * account's own numbers do not support paying for that: average watch is 4.6
 * seconds against a 25 second video, and in 1 of 53 posts did the average
 * viewer reach even the 55% mark. Moving the ask later costs almost no
 * exposure, because the people who get there are the ones who stayed, and it
 * buys back a quarter of the video with nothing sitting on top of it.
 *
 * Do not read that 4.6 seconds as "everyone quits at five". It averages in the
 * ~72% who skip inside three seconds, so it restates the skip rate rather than
 * describing a second problem.
 */
const CTA_FROM_FRACTION = 0.78;

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
const CallToAction: React.FC = () => {
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
          <span style={{ color: theme.color.accent }}>Follow</span>
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
          new one every night
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const Reel: React.FC<VideoSpec> = (spec) => {
  const { fps } = useVideoConfig();
  // Where the ask appears. It no longer takes anything from the captions, so
  // they run to the last frame and the video ends on content.
  const ctaFrom = spec.showFollowCta
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
          <OpeningSceneContext.Provider value={scene.fromFrame === 0}>
            <SceneRenderer
              scene={scene}
              repo={spec.repo}
              pageSrc={spec.pageSrc}
              pageAspect={spec.pageAspect}
            />
          </OpeningSceneContext.Provider>
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
          the hook.

          showFollowCta false renders the same video with no ask at all. Nothing
          sets it today: all four destinations take this file, ask included. It
          stays because a version without the ask can only ever be a second
          render and never a cut of this one, an ask visible from the middle
          being unable to be absent from a truncation of the same file. */}
      {spec.showFollowCta && ctaFrom !== undefined ? (
        <Sequence
          from={ctaFrom}
          durationInFrames={spec.durationInFrames - ctaFrom}
          layout="none"
        >
          <CallToAction />
        </Sequence>
      ) : null}

      {spec.audioSrc ? <Audio src={staticFile(spec.audioSrc)} /> : null}
    </AbsoluteFill>
  );
};
