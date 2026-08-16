/**
 * Runtime mirror of pipeline/models.py VideoSpec.
 *
 * This is the contract enforcement the Python docstring promises. Every field
 * here corresponds one-to-one to a field on the Pydantic side, and the spec is
 * parsed through it in calculateMetadata before a single frame is rendered.
 *
 * Why runtime and not just types: video.json is produced by a separate process
 * in a different language. TypeScript checks the code we wrote, not the JSON we
 * were handed. Without this, renaming a field on the Python side surfaces as an
 * `undefined` painted into the middle of a finished MP4 -- a 3-minute render
 * that looks fine to the pipeline and wrong to a human. Now it fails in the
 * first second, naming the field.
 *
 * `types.ts` derives its types from these schemas, so there is exactly one
 * definition per shape.
 */

import { z } from "zod";

export const cueKindSchema = z.enum([
  "repo_card",
  "code",
  "stat",
  "bullets",
  "terminal",
  "screenshot",
]);

/** One Shiki-highlighted token. Produced in calculateMetadata, never by Python. */
export const codeTokenSchema = z.object({
  content: z.string(),
  color: z.string(),
});

export const sceneSchema = z.object({
  kind: cueKindSchema,
  fromFrame: z.number().int().nonnegative(),
  durationInFrames: z.number().int().positive(),
  title: z.string().nullable().optional(),
  subtitle: z.string().nullable().optional(),
  bullets: z.array(z.string()).default([]),
  code: z.string().nullable().optional(),
  codeLanguage: z.string().nullable().optional(),
  statValue: z.string().nullable().optional(),
  statLabel: z.string().nullable().optional(),
  /** Path relative to video/public/, for screenshot scenes. */
  imageSrc: z.string().nullable().optional(),
  /** Injected by calculateMetadata; absent in the JSON Python writes. */
  tokens: z.array(z.array(codeTokenSchema)).optional(),
});

export const captionSchema = z.object({
  text: z.string(),
  startMs: z.number(),
  endMs: z.number(),
  timestampMs: z.number().nullable().optional(),
  confidence: z.number().nullable().optional(),
});

export const repoMetaSchema = z.object({
  fullName: z.string(),
  owner: z.string(),
  name: z.string(),
  stars: z.number().int(),
  starsGainedToday: z.number().int().nullable().optional(),
  language: z.string().nullable().optional(),
  license: z.string().nullable().optional(),
  url: z.string(),
});

export const videoSpecSchema = z.object({
  // Bumped by hand on a breaking change to the contract. Pinned rather than
  // open so an older renderer refuses a newer spec instead of half-rendering it.
  version: z.literal(1),
  slug: z.string().min(1),
  // Pydantic serialises `date` as a plain ISO day, not a full timestamp.
  createdOn: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "expected an ISO date, e.g. 2026-07-30"),

  width: z.number().int().positive(),
  height: z.number().int().positive(),
  fps: z.number().int().positive(),
  durationInFrames: z.number().int().positive(),

  hook: z.string(),
  /** Path relative to video/public/. Empty in the Studio placeholder. */
  audioSrc: z.string(),
  repo: repoMetaSchema,
  scenes: z.array(sceneSchema),
  captions: z.array(captionSchema),
  /**
   * The word to comment for the link, shown as an end card. Null when no
   * gateway is configured: asking for a comment nothing listens for is a
   * promise the account cannot keep.
   */
  ctaKeyword: z.string().nullable().optional(),
  /**
   * The frame the ask begins on, so a surface with no way to deliver it can
   * cut the video there. Nothing in the render reads this: it is carried so
   * the spec stays the single description of the video, rather than a trim
   * re-deriving a boundary the spec has already decided.
   *
   * Null when there is no ask, and when the split was refused for landing too
   * close to a scene boundary.
   */
  ctaFromFrame: z.number().int().nullable().optional(),
});

/**
 * Parse, or throw with every mismatch listed at once.
 *
 * Zod's own message is a JSON blob that Remotion prints on one line; this
 * rewrites it as `field: problem` lines so a drifted contract is readable in
 * the render log without scrolling sideways.
 */
export function parseVideoSpec(input: unknown): z.infer<typeof videoSpecSchema> {
  const result = videoSpecSchema.safeParse(input);
  if (result.success) return result.data;

  const problems = result.error.issues
    .map((issue) => `  ${issue.path.join(".") || "(root)"}: ${issue.message}`)
    .join("\n");
  throw new Error(
    `video.json does not match the VideoSpec contract.\n${problems}\n\n` +
      `This means pipeline/models.py and video/src/schema.ts have drifted apart. ` +
      `Change both, or regenerate the spec with the current pipeline.`,
  );
}
