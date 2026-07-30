import React from "react";
import { Img, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";

import { theme } from "../theme";

type Props = {
  src: string;
  url: string;
};

/**
 * The repo screenshot inside browser chrome, with a slow push-in.
 *
 * The screenshot is not meant to be read -- at 1080 wide, GitHub's 14px body
 * text lands around 10px. It is there to be *recognised*: two seconds of a
 * real GitHub page establishes that this is a real project before the
 * voiceover has said anything. The hook text sits on top of it.
 *
 * The slow zoom matters more than it looks. A held still image reads as a
 * slideshow; continuous motion reads as video.
 */
export const BrowserFrame: React.FC<Props> = ({ src, url }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  // Kept gentle: the push-in exists to stop the shot reading as a static
  // slide, not to be noticed. It also crops, and this frame holds the README
  // hero -- title lockup and badges -- so there is nothing here worth losing
  // to a more dramatic move.
  const zoom = interpolate(frame, [0, durationInFrames], [1.0, 1.03], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const drift = interpolate(frame, [0, durationInFrames], [0, -14], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <div
      style={{
        width: "100%",
        borderRadius: theme.radius,
        overflow: "hidden",
        border: `2px solid ${theme.color.border}`,
        backgroundColor: theme.color.surface,
        boxShadow: "0 40px 110px rgba(0,0,0,0.75)",
      }}
    >
      {/* Chrome: traffic lights + address bar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "20px 26px",
          backgroundColor: theme.color.surfaceRaised,
          borderBottom: `2px solid ${theme.color.border}`,
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

      {/* Screenshot, cropped by the frame as it zooms */}
      <div style={{ overflow: "hidden", lineHeight: 0 }}>
        <Img
          src={staticFile(src)}
          style={{
            width: "100%",
            display: "block",
            transform: `scale(${zoom}) translateY(${drift}px)`,
            transformOrigin: "top center",
          }}
        />
      </div>
    </div>
  );
};
