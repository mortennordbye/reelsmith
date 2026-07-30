/**
 * Mirror of pipeline/models.py VideoSpec.
 *
 * Keep these in sync. Python writes video.json; this file is what Remotion
 * expects to read. A field renamed on one side without the other will surface
 * as a TypeScript error or an undefined at render time.
 */

export type CueKind =
  | "repo_card"
  | "code"
  | "stat"
  | "bullets"
  | "terminal"
  | "screenshot";

/** One Shiki-highlighted token. Produced in calculateMetadata, never by Python. */
export type CodeToken = {
  content: string;
  color: string;
};

export type Scene = {
  kind: CueKind;
  fromFrame: number;
  durationInFrames: number;
  title?: string | null;
  subtitle?: string | null;
  bullets: string[];
  code?: string | null;
  codeLanguage?: string | null;
  statValue?: string | null;
  statLabel?: string | null;
  /** Path relative to video/public/, for screenshot scenes. */
  imageSrc?: string | null;
  /** Injected by calculateMetadata; absent in the JSON Python writes. */
  tokens?: CodeToken[][];
};

export type Caption = {
  text: string;
  startMs: number;
  endMs: number;
  timestampMs?: number | null;
  confidence?: number | null;
};

export type RepoMeta = {
  fullName: string;
  owner: string;
  name: string;
  stars: number;
  starsGainedToday?: number | null;
  language?: string | null;
  license?: string | null;
  url: string;
};

export type VideoSpec = {
  version: number;
  slug: string;
  createdOn: string;
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  hook: string;
  audioSrc: string;
  repo: RepoMeta;
  scenes: Scene[];
  captions: Caption[];
};
