import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";
import React from "react";
import { Composition, type CalculateMetadataFunction } from "remotion";

import { Reel } from "./Reel";
import { highlightScenes } from "./highlight";
import { parseVideoSpec } from "./schema";
import { theme } from "./theme";
import type { VideoSpec } from "./types";

// Loading at module scope means the fonts are ready before the first frame is
// rendered; doing it in a component causes a visible flash of fallback type.
const inter = loadInter();
const mono = loadMono();
(theme.font as { display: string }).display = `${inter.fontFamily}, sans-serif`;
(theme.font as { mono: string }).mono = `${mono.fontFamily}, monospace`;

/**
 * Placeholder shown in Remotion Studio before you load a real video.json.
 * Every field the renderer touches must be present, or the Studio errors on
 * open rather than showing a preview.
 */
const PLACEHOLDER: VideoSpec = {
  version: 1,
  slug: "placeholder",
  createdOn: "2026-01-01",
  width: 1080,
  height: 1920,
  fps: 30,
  durationInFrames: 900,
  hook: "Load a real video.json to preview",
  audioSrc: "",
  repo: {
    fullName: "owner/repo",
    owner: "owner",
    name: "repo",
    stars: 12345,
    starsGainedToday: 420,
    language: "Rust",
    license: "MIT",
    url: "https://github.com/owner/repo",
  },
  scenes: [
    {
      kind: "repo_card",
      fromFrame: 0,
      durationInFrames: 300,
      title: "repo",
      subtitle: "A one-line description of the project.",
      bullets: [],
    },
    {
      kind: "terminal",
      fromFrame: 300,
      durationInFrames: 300,
      bullets: [],
      code: "cargo install repo\nrepo --help",
      codeLanguage: "bash",
    },
    {
      kind: "stat",
      fromFrame: 600,
      durationInFrames: 300,
      bullets: [],
      statValue: "80x",
      statLabel: "faster than the alternative",
    },
  ],
  captions: [],
};

/**
 * Runs in Node before rendering. Three jobs:
 *   1. Validate the incoming spec against the zod contract, so a drift between
 *      pipeline/models.py and this side fails in the first second rather than
 *      painting `undefined` into a finished MP4.
 *   2. Adopt the dimensions/duration from the loaded spec rather than the
 *      Composition defaults, so one composition serves every video.
 *   3. Syntax-highlight code once, instead of per frame in the browser.
 */
const calculateMetadata: CalculateMetadataFunction<VideoSpec> = async ({ props }) => {
  const spec = parseVideoSpec(props);
  const scenes = await highlightScenes(spec.scenes);
  return {
    durationInFrames: spec.durationInFrames,
    fps: spec.fps,
    width: spec.width,
    height: spec.height,
    props: { ...spec, scenes },
  };
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Reel"
    component={Reel}
    durationInFrames={PLACEHOLDER.durationInFrames}
    fps={PLACEHOLDER.fps}
    width={PLACEHOLDER.width}
    height={PLACEHOLDER.height}
    defaultProps={PLACEHOLDER}
    calculateMetadata={calculateMetadata}
  />
);
