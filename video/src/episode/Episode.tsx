import React from "react";
import { AbsoluteFill, Audio, Sequence, staticFile } from "remotion";

import type { EpisodeSpec } from "../episodeSchema";
import { EndCard, Grain, Hook, Line, Scan, Source } from "./parts";
import { paper } from "./theme";

/**
 * One episode of the second niche, assembled from a spec.
 *
 * The shots are the script's own lines, cut where the voice actually ended
 * them, so nothing here chooses a duration. What this file decides is only how
 * a line looks, which is the half a spec cannot carry.
 */
export const Episode: React.FC<EpisodeSpec> = (spec) => {
  const spoken = spec.shots.reduce(
    (last, shot) => Math.max(last, shot.start + shot.durationInFrames),
    0,
  );

  return (
    <AbsoluteFill style={{ background: paper.ground }}>
      {spec.audioSrc ? <Audio src={staticFile(spec.audioSrc)} /> : null}

      {spec.shots.map((shot, i) => {
        const artefact = spec.artefacts[shot.art] ?? spec.artefacts[0];
        return (
          <Sequence key={i} from={shot.start} durationInFrames={shot.durationInFrames}>
            <Scan artefact={artefact} crop={shot.crop} fit={shot.fit} />
            {/* The hook replaces the first line's caption rather than sitting
                on top of it: two pieces of text in the first three seconds is
                the one place this format cannot afford to be busy. */}
            {i === 0 ? <Hook text={spec.hook} /> : <Line shot={shot} />}
            {shot.kind === "quote" ? <Source text={spec.source.slice(0, 90)} /> : null}
            <Grain />
          </Sequence>
        );
      })}

      <Sequence from={spoken}>
        <EndCard
          name={spec.endcardName}
          handle={spec.endcardHandle}
          tagline={spec.endcardTagline}
        />
      </Sequence>
    </AbsoluteFill>
  );
};
