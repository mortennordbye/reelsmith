/**
 * Runtime mirror of pipeline/models.py EpisodeSpec.
 *
 * The same contract enforcement `schema.ts` provides for the reel, and for the
 * same reason: episode.json is written by another process in another language,
 * so TypeScript checks the code we wrote and not the JSON we were handed.
 * Without this a renamed field is an `undefined` painted into a finished MP4.
 *
 * A separate file rather than more shapes in `schema.ts`, because the two specs
 * share no field that means the same thing.
 */

import { z } from "zod";

export const specArtefactSchema = z.object({
  src: z.string(),
  w: z.number().int().positive(),
  h: z.number().int().positive(),
  title: z.string().default(""),
  licence: z.string().default(""),
  credit: z.string().default(""),
});

export const cropSchema = z.object({
  sx: z.number().int().nonnegative(),
  sy: z.number().int().nonnegative(),
  sw: z.number().int().positive(),
  sh: z.number().int().positive(),
});

export const shotSchema = z.object({
  start: z.number().int().nonnegative(),
  durationInFrames: z.number().int().positive(),
  line: z.string(),
  kind: z.string(),
  art: z.number().int().nonnegative().default(0),
  crop: cropSchema.nullish(),
  fit: z.string().default("cover"),
  step: z.number().int().nullish(),
});

export const episodeSpecSchema = z.object({
  version: z.number().int().default(1),
  slug: z.string(),
  createdOn: z.string(),
  width: z.number().int().positive().default(1080),
  height: z.number().int().positive().default(1920),
  fps: z.number().int().positive().default(30),
  durationInFrames: z.number().int().positive(),
  hook: z.string(),
  audioSrc: z.string(),
  subject: z.string(),
  lived: z.string().default(""),
  source: z.string(),
  artefacts: z.array(specArtefactSchema).min(1),
  shots: z.array(shotSchema).min(1),
  endcardName: z.string().default(""),
  endcardHandle: z.string().default(""),
  endcardTagline: z.string().default(""),
});

export type EpisodeSpec = z.infer<typeof episodeSpecSchema>;
export type Shot = z.infer<typeof shotSchema>;
export type SpecArtefact = z.infer<typeof specArtefactSchema>;

export function parseEpisodeSpec(input: unknown): EpisodeSpec {
  const parsed = episodeSpecSchema.safeParse(input);
  if (!parsed.success) {
    throw new Error(`episode.json does not match the spec: ${parsed.error.message}`);
  }
  return parsed.data;
}
