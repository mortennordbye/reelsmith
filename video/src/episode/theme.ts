/**
 * The second niche's palette, which is not the reel's.
 *
 * Account 1 is GitHub dark on purpose, because that audience stares at that
 * colour scheme all day and it reads as native. This one is paper: the shot is
 * a four hundred year old scan, and a dark UI chrome around it would announce
 * that the page has been imported into something. The ground is the same off
 * white the scans already are.
 *
 * Verdigris is the accent for the same reason the avatar uses it. It is the
 * colour of a stamp on old paper, and it is nowhere near any platform's brand
 * hue, so nothing on screen reads as a logo.
 */
export const paper = {
  ground: "#f2eee5",
  ink: "#17150f",
  inkDim: "#5b5449",
  verdigris: "#2f6b5e",
  oxblood: "#7d2f2a",
  rule: "#cbc3b3",
} as const;

export const font = {
  display: "sans-serif",
  source: "Georgia, serif",
  mono: "monospace",
};
