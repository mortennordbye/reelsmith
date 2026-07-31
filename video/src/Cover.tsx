/**
 * The Reels cover still.
 *
 * Every frame of the video itself carries either the hook or a burned-in
 * caption, so there is nothing clean to grab in Instagram's cover picker. This
 * renders a dedicated frame instead, from the same spec and the same theme, so
 * the cover cannot drift from the video it fronts.
 *
 * Two variants, chosen with `showHook`:
 *   true   the shipping cover, hook set inside the crop-safe band
 *   false  the screenshot alone, for hand-designing a cover over the top
 */

import React from "react";
import { AbsoluteFill } from "remotion";

import { Background } from "./components/Background";
import { SceneRenderer } from "./scenes/SceneRenderer";
import { theme } from "./theme";
import type { VideoSpec } from "./types";

export type CoverProps = VideoSpec & { showHook?: boolean };

/**
 * The point of the cover is the README hero, so the hook sits in the empty band
 * below the browser frame rather than on top of it. Centring the text was the
 * obvious first try and it buried the screenshot the cover exists to show.
 *
 * Instagram crops a 1080x1920 cover to a centred 4:5 (1080x1350, y 285 to 1635)
 * for the profile grid, so the text has to finish above 1635 to survive it.
 * That is what the bottom padding buys, and why the type is smaller here than
 * the in-video hook: four lines at the video's size would not fit the gap.
 */
const HOOK_BOTTOM = 300;
const HOOK_SIZE = 66;

export const Cover: React.FC<CoverProps> = ({ showHook = true, ...spec }) => {
  // The opening scene is the README hero when the screenshot survived, and the
  // repo card when it did not. Either is a reasonable cover, and taking
  // scenes[0] means this never has to know which one it got.
  const opening = spec.scenes[0];

  return (
    <AbsoluteFill style={{ backgroundColor: theme.color.bgDeep }}>
      <Background />

      {opening ? <SceneRenderer scene={opening} repo={spec.repo} /> : null}

      {showHook ? (
        <AbsoluteFill
          style={{
            justifyContent: "flex-end",
            alignItems: "center",
            paddingBottom: HOOK_BOTTOM,
            paddingLeft: theme.padding,
            paddingRight: theme.padding,
          }}
        >
          <span
            style={{
              fontFamily: theme.font.display,
              fontSize: HOOK_SIZE,
              fontWeight: 800,
              lineHeight: 1.12,
              letterSpacing: "-0.02em",
              color: theme.color.text,
              textAlign: "center",
              display: "block",
              textWrap: "balance",
              // No panel behind it. The band below the browser frame is already
              // near-black, so a plain shadow is enough to hold the text off the
              // background without adding another box to the composition.
              textShadow: "0 4px 28px rgba(1, 4, 9, 0.95)",
            }}
          >
            {spec.hook}
          </span>
        </AbsoluteFill>
      ) : null}

      {/* The repo name used to be printed across the top here, as it was on the
          Reel. Both are gone: the name belongs in the README shot and the
          voiceover, and nowhere else. Repeating it in chrome is what made the
          comment unnecessary, and the comment is the whole mechanic. */}
    </AbsoluteFill>
  );
};
