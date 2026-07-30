/**
 * Single source of visual truth.
 *
 * Palette is GitHub-dark-adjacent on purpose: this audience stares at that
 * exact colour scheme all day, so it reads as native rather than as "a video
 * about code".
 */

export const theme = {
  color: {
    bg: "#0D1117",
    bgDeep: "#010409",
    surface: "#161B22",
    surfaceRaised: "#1C2129",
    border: "#30363D",
    text: "#E6EDF3",
    muted: "#8B949E",
    accent: "#58A6FF",
    accentWarm: "#F0883E",
    success: "#3FB950",
    star: "#E3B341",
  },
  font: {
    // Filled in at runtime by loadFonts() in Root.tsx.
    display: "Inter, -apple-system, system-ui, sans-serif",
    mono: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
  },
  size: {
    // Tuned for a 1080x1920 frame viewed on a phone at arm's length.
    hook: 104,
    caption: 76,
    sceneTitle: 68,
    sceneSubtitle: 38,
    bullet: 46,
    code: 34,
    stat: 190,
    statLabel: 40,
    meta: 32,
  },
  padding: 88,
  radius: 28,
} as const;

/** Shiki theme kept in step with the palette above. */
export const SHIKI_THEME = "github-dark-default";
