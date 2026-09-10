import React from "react";
import { AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";

import { browserChromeHeight, safeTop, sceneSafeBottom, theme } from "../theme";

/** How far above the caption safe area the page starts fading. */
const FADE_RUNUP = 260;

type Props = {
  src: string;
  aspect: number;
  url: string;
};

/**
 * The README, full bleed, scrolling.
 *
 * This replaces a browser card floating in the middle of the frame with a
 * third of the frame left empty above and below it. The empty band was not a
 * composition choice, it was what a 16:10 screenshot leaves behind when it is
 * dropped into 9:16, and in a format where the frame is the entire product it
 * was a third of the product spent on nothing.
 *
 * **The motion is the page moving, not an effect applied to a still.** That
 * distinction is the whole reason this exists. Motion graphics are cheap to
 * automate, which is exactly why every generated channel has them and why they
 * read as generated; a page scrolling is the artifact itself doing the only
 * thing a page does. The old push-in was an effect applied to a still, and it
 * was there to stop the shot reading as a slideshow, which is the same problem
 * this solves by showing more of the actual thing.
 */
export const ReadmePage: React.FC<Props> = ({ src, aspect, url }) => {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  // The image is laid out at full frame width, so its rendered height follows
  // from the aspect the capture recorded. Carried rather than measured: a
  // layout read inside a frame render is how one frame ends up different from
  // its neighbour.
  const rendered = width * aspect;
  const scrollable = Math.max(rendered - height, 0);

  // Bounded by speed as well as by the page. A long README would otherwise
  // scroll proportionally faster in a short scene, which is backwards: the
  // speed a page can be read at does not depend on how much of it there is.
  // 96px per second at 1080 wide is about a line of GitHub body text a second.
  const seconds = durationInFrames / fps;
  const travel = Math.min(scrollable, 96 * seconds);

  const y = interpolate(frame, [0, durationInFrames], [0, -travel], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ backgroundColor: theme.color.bg, overflow: "hidden" }}>
      <Img
        src={staticFile(src)}
        style={{
          position: "absolute",
          top: safeTop + browserChromeHeight,
          left: 0,
          width: "100%",
          display: "block",
          transform: `translateY(${y}px)`,
        }}
      />

      {/*
        The chrome stays. It is the cheapest signal that this is a real page
        rather than something we drew, and it is what the address bar is for:
        the repo name, never the full URL, for the reason BrowserFrame gives.
        Pinned above the scroll so it does not travel off the top and take the
        signal with it.
      */}
      <div
        style={{
          position: "absolute",
          // Below the platform's own chrome, not at y=0. A browser window
          // whose title bar is sliced off by the top of the screen reads as a
          // rendering fault rather than as a design; see `safeTop`.
          top: safeTop,
          left: 0,
          right: 0,
          height: browserChromeHeight,
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "0 26px",
          backgroundColor: theme.color.surfaceRaised,
          borderBottom: `2px solid ${theme.color.border}`,
          // Rounded at the top only. The window is inset from the top of the
          // frame and bleeds off the bottom, so it has two corners and not
          // four, and squaring them is what made the inset look like a crop.
          borderTopLeftRadius: theme.radius,
          borderTopRightRadius: theme.radius,
        }}
      >
        {["#FF5F57", "#FEBC2E", "#28C840"].map((c) => (
          <div key={c} style={{ width: 18, height: 18, borderRadius: 9, backgroundColor: c }} />
        ))}
        <div
          style={{
            flex: 1,
            marginLeft: 14,
            padding: "10px 20px",
            borderRadius: 999,
            backgroundColor: theme.color.bg,
            fontFamily: theme.font.mono,
            fontSize: 26,
            color: theme.color.muted,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {url}
        </div>
      </div>

      {/*
        The page fades out behind the caption band rather than being cut off by
        it. A hard edge under a line of burnt-in text reads as two layers that
        do not know about each other.

        Sized from `sceneSafeBottom`, which is the same number every framed
        scene uses to stay clear of the captions, plus a run-up to fade over.
        Guessing it is what the derivation in theme.ts exists to stop: the
        first attempt used a flat 520 and the captions landed on the part of
        the gradient that was still transparent, which is the exact failure
        that comment is about.

        Solid by the time the tallest phrase starts, so a three line caption is
        read against the background rather than against a paragraph of README.
      */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 0,
          height: sceneSafeBottom + FADE_RUNUP,
          // Dimmed to 0.93, not painted out. Solid black below the caption
          // left a dead band between the last readable line and the words, and
          // a full-bleed shot that stops two thirds of the way down is the
          // same wasted frame this component was written to recover, just
          // moved to the other end. At 0.93 the page is still faintly there,
          // which reads as one continuous shot, and the caption still has the
          // contrast it needs.
          background: `linear-gradient(180deg, rgba(1,4,9,0) 0%, rgba(1,4,9,0.93) ${
            (FADE_RUNUP / (sceneSafeBottom + FADE_RUNUP)) * 100
          }%)`,
        }}
      />
    </AbsoluteFill>
  );
};

