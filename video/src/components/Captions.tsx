import { createTikTokStyleCaptions, type Caption as RemotionCaption } from "@remotion/captions";
import React, { useMemo } from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

import { captionBand, theme } from "../theme";
import type { Caption } from "../types";

/** Words per on-screen group. Whole phrases read far better than one word at a
 *  time, which forces the eye to re-fixate constantly. */
const GROUP_MS = 900;

type Props = {
  captions: Caption[];
};

export const Captions: React.FC<Props> = ({ captions }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const nowMs = (frame / fps) * 1000;

  // Grouping word tokens into readable phrases is genuinely fiddly (it has to
  // respect punctuation, max width, and pauses), so use Remotion's own
  // implementation rather than reinventing it.
  const pages = useMemo(() => {
    const source: RemotionCaption[] = captions.map((c) => ({
      text: c.text.startsWith(" ") ? c.text : ` ${c.text}`,
      startMs: c.startMs,
      endMs: c.endMs,
      timestampMs: c.timestampMs ?? (c.startMs + c.endMs) / 2,
      confidence: c.confidence ?? null,
    }));
    return createTikTokStyleCaptions({ captions: source, combineTokensWithinMilliseconds: GROUP_MS })
      .pages;
  }, [captions]);

  const page = pages.find(
    (p) => nowMs >= p.startMs && nowMs < p.startMs + p.durationMs,
  );
  if (!page) return null;

  const enterProgress = spring({
    frame: frame - (page.startMs / 1000) * fps,
    fps,
    config: { damping: 200, stiffness: 220, mass: 0.5 },
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        // Sits above Instagram's bottom UI chrome so captions are never
        // covered by the like/comment column or the caption preview.
        paddingBottom: captionBand.fromBottom,
        paddingLeft: theme.padding,
        paddingRight: theme.padding,
      }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: "0 18px",
          transform: `scale(${interpolate(enterProgress, [0, 1], [0.92, 1])})`,
        }}
      >
        {page.tokens.map((token, i) => {
          const isActive = nowMs >= token.fromMs && nowMs < token.toMs;
          return (
            <span
              key={`${token.fromMs}-${i}`}
              style={{
                fontFamily: theme.font.display,
                fontSize: theme.size.caption,
                fontWeight: 800,
                letterSpacing: "-0.02em",
                lineHeight: captionBand.lineHeight,
                color: isActive ? theme.color.accent : theme.color.text,
                // A hard shadow keeps text legible over any background,
                // which matters because we never control what's behind it.
                textShadow:
                  "0 4px 20px rgba(0,0,0,0.85), 0 0 3px rgba(0,0,0,0.95)",
                transform: isActive ? "translateY(-6px)" : "none",
                transition: "none",
              }}
            >
              {token.text.trim()}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
