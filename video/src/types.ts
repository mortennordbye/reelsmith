/**
 * Types for the render spec.
 *
 * Every one of these is inferred from the zod schemas in schema.ts, which are
 * in turn the mirror of pipeline/models.py VideoSpec. Editing a shape means
 * editing schema.ts; this file only re-exports, so the compile-time types and
 * the runtime validation can never disagree.
 */

import type { z } from "zod";

import type {
  captionSchema,
  codeTokenSchema,
  cueKindSchema,
  repoMetaSchema,
  sceneSchema,
  videoSpecSchema,
} from "./schema";

export type CueKind = z.infer<typeof cueKindSchema>;
export type CodeToken = z.infer<typeof codeTokenSchema>;
export type Scene = z.infer<typeof sceneSchema>;
export type Caption = z.infer<typeof captionSchema>;
export type RepoMeta = z.infer<typeof repoMetaSchema>;
export type VideoSpec = z.infer<typeof videoSpecSchema>;
