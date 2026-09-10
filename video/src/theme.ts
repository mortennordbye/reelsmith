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
    // The hook in its top strip. Smaller than the centred hook, because a
    // strip has a width budget rather than the whole frame: at 104 most hooks
    // wrapped to four lines and pushed down onto the hero the move exists to
    // uncover.
    hookStrip: 72,
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

/**
 * The band at the bottom of the frame that captions and the end card own.
 *
 * Both of them sit here because it is the only strip Instagram's own chrome
 * does not cover, and scene content has to stay out of it. That used to be two
 * unrelated numbers in two files, and they disagreed: the captions grow upward
 * from their baseline, so a three line phrase climbed 88px into the scene above
 * and printed straight over a bullet list.
 *
 * Derived rather than guessed, so changing the caption size cannot silently
 * reintroduce the overlap.
 */
export const captionBand = {
  /** Distance from the bottom of the frame to the caption baseline. Clears the
   *  like and comment column and the caption preview. */
  fromBottom: 430,
  /** Worst case phrase height. 900ms of speech reliably wraps to three lines at
   *  this size, and reserving for it costs the scene 130px it was not using. */
  maxLines: 3,
  /** Breathing room between the tallest phrase and whatever is above it. */
  gap: 40,
  lineHeight: 1.22,
} as const;

/**
 * The band at the top of the frame that the platform's own chrome covers.
 *
 * The counterpart to `captionBand`, and it existed as a bare `paddingTop: 300`
 * in `SceneRenderer` for as long as every scene was a card floating in the
 * middle of the frame. A full-bleed shot has no padding to hide behind, so the
 * number had to become a shared one: the browser chrome in `ReadmePage` sat at
 * y=0 and read as a window sliced off by the top of the screen, which looks
 * broken rather than deliberate.
 *
 * Instagram puts the back arrow and the account name here, TikTok its
 * Following/For You tabs, and a notched phone loses more again. 230 clears all
 * three and leaves the chrome bar's bottom edge at 326, which is where scene
 * content has effectively started all along.
 *
 * Nothing that has to be recognised may sit above this line.
 */
export const safeTop = 230;

/**
 * The drawn browser title bar in `ReadmePage`: traffic lights and address bar,
 * and no taller. In theme rather than beside its component because the hook
 * strip has to start below it, and two files each holding their own idea of
 * how tall it is are two files that disagree the first time one changes.
 */
export const browserChromeHeight = 96;

/** How much bottom padding a scene needs to stay clear of the caption band. */
export const sceneSafeBottom =
  captionBand.fromBottom +
  Math.ceil(theme.size.caption * captionBand.lineHeight * captionBand.maxLines) +
  captionBand.gap;

/** Shiki theme kept in step with the palette above. */
export const SHIKI_THEME = "github-dark-default";
