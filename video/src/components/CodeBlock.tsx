import React from "react";
import { interpolate, useCurrentFrame } from "remotion";

import { theme } from "../theme";
import type { CodeToken } from "../types";

type Props = {
  tokens: CodeToken[][];
  /** Renders a macOS-style title bar with a prompt, for install commands. */
  terminal?: boolean;
  filename?: string | null;
};

/**
 * An IDE-looking code card.
 *
 * Lines stagger in one at a time. That is the single highest-value animation
 * in the whole project: it gives the eye somewhere to go and makes a static
 * snippet feel like something is happening.
 */
// Card inner width at 1080px: frame - 2*stage padding - 2*card padding.
const CODE_AREA_PX = 1080 - 2 * theme.padding - 2 * 36;
// JetBrains Mono advance width is very close to 0.6em.
const MONO_ADVANCE = 0.6;

export const CodeBlock: React.FC<Props> = ({ tokens, terminal = false, filename }) => {
  const frame = useCurrentFrame();

  // Shrink to fit the longest line rather than letting it run off the card.
  // A long install URL is exactly the case that breaks a fixed font size, and
  // it is also the single most common snippet in this niche.
  const longestLine = tokens.reduce((max, line) => {
    const len = line.reduce((n, t) => n + t.content.length, 0) + (terminal ? 2 : 0);
    return Math.max(max, len);
  }, 0);
  const fontSize = Math.min(
    theme.size.code,
    longestLine > 0 ? CODE_AREA_PX / (longestLine * MONO_ADVANCE) : theme.size.code,
  );

  return (
    <div
      style={{
        width: "100%",
        borderRadius: theme.radius,
        overflow: "hidden",
        border: `2px solid ${theme.color.border}`,
        backgroundColor: theme.color.surface,
        boxShadow: "0 30px 90px rgba(0,0,0,0.6)",
      }}
    >
      {/* Title bar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "22px 30px",
          backgroundColor: theme.color.surfaceRaised,
          borderBottom: `2px solid ${theme.color.border}`,
        }}
      >
        {["#FF5F57", "#FEBC2E", "#28C840"].map((c) => (
          <div
            key={c}
            style={{ width: 20, height: 20, borderRadius: 10, backgroundColor: c }}
          />
        ))}
        <span
          style={{
            marginLeft: 12,
            fontFamily: theme.font.mono,
            fontSize: theme.size.meta,
            color: theme.color.muted,
          }}
        >
          {filename ?? (terminal ? "zsh" : "")}
        </span>
      </div>

      {/* Code body */}
      <div style={{ padding: "34px 36px", display: "flex", flexDirection: "column" }}>
        {tokens.map((line, lineIndex) => {
          // 3 frames of stagger per line: fast enough not to hold up the
          // narration, slow enough to read as sequential.
          const appear = interpolate(
            frame,
            [lineIndex * 3, lineIndex * 3 + 9],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
          );
          const isBlank = line.reduce((n, t) => n + t.content.trim().length, 0) === 0;
          return (
            <div
              key={lineIndex}
              style={{
                fontFamily: theme.font.mono,
                fontSize,
                lineHeight: 1.62,
                whiteSpace: "pre",
                // JetBrains Mono ligatures fuse operators into single glyphs,
                // which is pleasant in an editor and wrong here: `<!--` renders
                // as an arrow and `-->` as a long dash, so an HTML comment stops
                // looking like one. The viewer has ~2 seconds to recognise the
                // snippet, and it has to match what they would type.
                fontVariantLigatures: "none",
                fontFeatureSettings: '"liga" 0, "calt" 0',
                opacity: appear,
                transform: `translateX(${interpolate(appear, [0, 1], [-14, 0])}px)`,
              }}
            >
              {/* In a terminal every command gets its own prompt -- install
                  snippets are usually several independent commands. */}
              {terminal && !isBlank ? (
                <span style={{ color: theme.color.success }}>$ </span>
              ) : null}
              {line.length === 0 ? (
                <span>&nbsp;</span>
              ) : (
                line.map((token, i) => (
                  <span key={i} style={{ color: token.color }}>
                    {token.content}
                  </span>
                ))
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
