---
name: reel-council
description: Brainstorm and stress test ideas for improving the Reels, using a panel of fixed perspectives that argue from this account's own numbers rather than from general short-form advice. Reads the real results, the real frames and the real code before proposing anything, scores each idea by effect, confidence, cost and blast radius, and files survivors in IDEAS.md. Use when asking "how do we make the video better", "why is retention bad", or "what should we try next".
---

# Reel council

A panel that proposes changes to the video, then tries to kill them.

The point is not to generate ideas. Ideas are free and most of them are the same
five effects every automated channel already ships. The point is to produce a
short ranked list where each entry names the number it should move, the file it
changes, and the evidence that would show it failed.

## Before anyone speaks

**No member may propose anything before this is done.** Every wrong turn this
process exists to prevent came from reasoning about the video from memory.

1. **Read the numbers.**
   `curl -s -H "Authorization: Bearer $GATEWAY_TOKEN" "$GATEWAY_URL/api/results"`
   gives views, reach, `skip_rate` and `avg_watch_ms` per post. Join hooks from
   `build/*/*/script.json` on the folder slug. State `n` out loud.
2. **Look at the frames.** Decode a real `out.mp4` with PyAV and build a contact
   sheet at frames 0, 3, 10, 30, 90 and a few later. `skip_rate` scores the
   first three seconds, so those are the frames that matter. Do not reason about
   what the opening looks like. Look at it.
3. **Read the code that draws it.** `video/src/Reel.tsx`,
   `video/src/scenes/SceneRenderer.tsx`, `video/src/components/`, and
   `SYSTEM_PROMPT` in `pipeline/scriptwriter.py`.
4. **Read `IDEAS.md` and `notes/*.md`.** Both are gitignored, so a fresh clone
   has neither and a fresh session will not think to look. Anything already
   tried, rejected or in flight is not a new idea, and the reason it was
   rejected is usually still true. The notes carry prior research runs and their
   *Still open* sections, which are the highest value part of the repository for
   this skill and the easiest thing in it to miss. The first council session
   proposed two things already sitting in a research note from ten days earlier.

Three separate proposals have been made in this project for things the pipeline
already had. Checking costs one grep.

**Do not assume `skip_rate` is the only thing worth moving.** It is the metric
the pipeline happens to collect and the one every analysis here reaches for
first, which is not the same as it being the metric that decides reach. Before
optimising it again, check what else the gateway already stores and what the
research notes claim actually drives distribution. Being able to measure
something is not evidence that it matters most.

## The council

Run each as its own pass. They may be subagents in parallel or sections of one
sitting; what matters is that each argues only its own lens and does not soften
to reach agreement. Disagreement is the output.

**The retention analyst.** Owns `skip_rate` and nothing else. Cares only about
the first three seconds. Must cite this account's numbers, not benchmarks from
elsewhere. There is a skip rate threshold below which median views jump several
fold; `IDEAS.md` records where it currently sits, and that is the target.

**The art director.** Owns composition, typography, hierarchy and restraint.
Argues for subtraction before addition. Asks what the frame would look like with
one element removed rather than one added.

**The sceptical engineer.** The actual audience: writes software, has seen a
thousand generated videos, stops watching the moment they clock one tell. Holds
a veto. If this member says an idea reads as machine made, it does not ship on
someone else's enthusiasm.

**The pipeline engineer.** Owns feasibility and blast radius. Labels every idea:
*prompt only* (text in `scriptwriter.py`), *one component* (a single file under
`video/src/`), or *cross stage* (touches `pipeline/models.py`, which means
mirroring in `video/src/schema.ts` and is the only category that can break the
interface between stages). Rejects nothing for being hard, but prices it.

**The conversion strategist.** Owns what happens after the video: view to
profile to follow. Holds the panel to the fact that this account's views have
converted to followers at a rate far below what reach alone would predict, so an
idea that only wins reach changes nothing on its own.

**The red team.** Speaks last and only against. Takes the three highest ranked
ideas and makes the strongest case that each is wrong, already refuted by the
data, or would make things worse. An idea that survives this is worth building.

## Rules every idea must satisfy

Reject on the spot anything that fails these. They are all things that already
went wrong here.

- **Names its metric.** Which number moves, and what result would prove it did
  not work. "Feels more premium" is not an idea.
- **Names its file.** An idea nobody can locate in the repo is a mood.
- **Distinguishes measured from asserted.** Say which. `n` here is small and
  most micro rules are noise; hook length and "contains a number" were both
  tested and both were noise.
- **Is not already implemented.** Grep first.
- **Is not chrome.** Watermarks, logo bugs, intro bumpers, mascots and animated
  brand stings are the signature of reposter and content farm accounts. They
  make the "is this AI" problem worse, not better.
- **Does not fake platform UI.** Drawn-in Follow buttons, fake progress bars,
  fake comment overlays. Instagram already draws those, and a video drawing its
  own reads as engagement bait.
- **Is not justified by being easy to automate.** This is the most tempting bad
  argument in the room. Effects that are cheap for code are cheap for *every*
  automated channel, which is exactly why counting numbers, typewriter code and
  karaoke captions have become the shared signature of generated video. Cheap to
  automate is evidence a thing is saturated, not evidence it is good.

## What actually differentiates this account

Keep this in front of the panel, because it is what the expensive ideas are
protecting: original research per repo, a cloned real human voice, a sceptical
register that will quote independent numbers against a project's own claims, and
a palette this audience reads as native rather than as a video about code. These
are hard to automate, which is why almost nobody has them. Motion graphics
flourishes are easy, so everybody does.

## Grounding in what already exists

Once per few runs, survey what public short-form video generators and Remotion
template projects actually ship, and write the findings into `IDEAS.md` under
*Saturated*. The value is inverted: whatever turns out to be common is what this
account must not look like. Treat that list as the tell inventory.

## Scoring

Rank by expected effect times confidence, divided by cost, with blast radius as
a tiebreak toward the contained change. Confidence comes from `n` and from
whether the evidence is this account's or somebody's blog post.

Anything the sceptical engineer vetoed is out regardless of score. Record it in
`IDEAS.md` under *Rejected* with the reason, so it does not come back in three
weeks wearing a different name.

## Output

Update `IDEAS.md` in the repo root. Never replace it; merge into it, because the
rejection reasons are the most valuable part and they accumulate. Sections:

- **Now** — ranked, ready to build, each with metric, file and blast radius.
- **Next** — good ideas blocked on something, with the blocker named.
- **Rejected** — with the reason and the date. The most useful section.
- **Saturated** — tells inventoried from what everyone else ships.
- **Measured** — what shipped, when, and what the number did afterwards. An idea
  that shipped and did not move its metric belongs in *Rejected* with evidence.

Then report the top three to the user in the conversation, with the red team's
objection to each stated rather than hidden. Do not build anything without being
asked; this skill produces a decision, not a diff.
