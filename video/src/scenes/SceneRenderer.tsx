import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

import { BrowserFrame } from "../components/BrowserFrame";
import { CodeBlock } from "../components/CodeBlock";
import { theme } from "../theme";
import type { RepoMeta, Scene } from "../types";

/**
 * Every scene shares this frame: content is vertically centred in the upper
 * two-thirds, leaving the lower third clear for captions. Nothing here may
 * drift into the caption safe area.
 */
const Stage: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 200, stiffness: 160, mass: 0.6 } });

  return (
    <AbsoluteFill
      style={{
        paddingLeft: theme.padding,
        paddingRight: theme.padding,
        paddingBottom: 620, // caption safe area
        paddingTop: 300,
        justifyContent: "center",
        alignItems: "center",
        opacity: interpolate(enter, [0, 1], [0, 1]),
        transform: `translateY(${interpolate(enter, [0, 1], [26, 0])}px)`,
      }}
    >
      <div style={{ width: "100%" }}>{children}</div>
    </AbsoluteFill>
  );
};

const StarIcon: React.FC<{ size: number; color: string }> = ({ size, color }) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill={color}>
    <path d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z" />
  </svg>
);

const RepoCardScene: React.FC<{ scene: Scene; repo: RepoMeta }> = ({ scene, repo }) => (
  <Stage>
    <div
      style={{
        border: `2px solid ${theme.color.border}`,
        borderRadius: theme.radius,
        backgroundColor: theme.color.surface,
        padding: 60,
        boxShadow: "0 30px 90px rgba(0,0,0,0.6)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          marginBottom: 26,
        }}
      >
        <StarIcon size={40} color={theme.color.star} />
        <span
          style={{
            fontFamily: theme.font.mono,
            fontSize: theme.size.meta,
            color: theme.color.star,
            fontWeight: 700,
          }}
        >
          {repo.stars.toLocaleString("en-US")}
          {repo.starsGainedToday ? `  ·  +${repo.starsGainedToday.toLocaleString("en-US")} today` : ""}
        </span>
      </div>

      <div
        style={{
          fontFamily: theme.font.mono,
          fontSize: theme.size.meta,
          color: theme.color.muted,
        }}
      >
        {repo.owner}/
      </div>
      <div
        style={{
          fontFamily: theme.font.display,
          fontSize: theme.size.sceneTitle,
          fontWeight: 800,
          color: theme.color.text,
          letterSpacing: "-0.03em",
          lineHeight: 1.1,
          marginBottom: 22,
          wordBreak: "break-word",
        }}
      >
        {scene.title ?? repo.name}
      </div>

      {scene.subtitle ? (
        <div
          style={{
            fontFamily: theme.font.display,
            fontSize: theme.size.sceneSubtitle,
            color: theme.color.muted,
            lineHeight: 1.4,
            marginBottom: 30,
          }}
        >
          {scene.subtitle}
        </div>
      ) : null}

      <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
        {[repo.language, repo.license].filter(Boolean).map((tag) => (
          <span
            key={tag}
            style={{
              fontFamily: theme.font.mono,
              fontSize: 28,
              color: theme.color.accent,
              border: `2px solid ${theme.color.accent}55`,
              borderRadius: 999,
              padding: "10px 24px",
            }}
          >
            {tag}
          </span>
        ))}
      </div>
    </div>
  </Stage>
);

const BulletsScene: React.FC<{ scene: Scene }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  return (
    <Stage>
      {scene.title ? (
        <div
          style={{
            fontFamily: theme.font.display,
            fontSize: theme.size.sceneSubtitle,
            color: theme.color.muted,
            marginBottom: 34,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
          }}
        >
          {scene.title}
        </div>
      ) : null}
      <div style={{ display: "flex", flexDirection: "column", gap: 30 }}>
        {scene.bullets.map((bullet, i) => {
          const s = spring({
            frame: frame - i * 7,
            fps,
            config: { damping: 200, stiffness: 180, mass: 0.5 },
          });
          return (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 24,
                opacity: s,
                transform: `translateX(${interpolate(s, [0, 1], [-30, 0])}px)`,
              }}
            >
              <div
                style={{
                  width: 14,
                  height: 14,
                  borderRadius: 7,
                  backgroundColor: theme.color.accent,
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  fontFamily: theme.font.display,
                  fontSize: theme.size.bullet,
                  fontWeight: 600,
                  color: theme.color.text,
                  lineHeight: 1.3,
                }}
              >
                {bullet}
              </span>
            </div>
          );
        })}
      </div>
    </Stage>
  );
};

const StatScene: React.FC<{ scene: Scene }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const pop = spring({ frame, fps, config: { damping: 12, stiffness: 200, mass: 0.8 } });

  return (
    <Stage>
      <div style={{ textAlign: "center" }}>
        <div
          style={{
            fontFamily: theme.font.display,
            fontSize: theme.size.stat,
            fontWeight: 900,
            color: theme.color.accent,
            letterSpacing: "-0.05em",
            lineHeight: 1,
            transform: `scale(${interpolate(pop, [0, 1], [0.6, 1])})`,
            textShadow: `0 0 80px ${theme.color.accent}55`,
          }}
        >
          {scene.statValue}
        </div>
        {scene.statLabel ? (
          <div
            style={{
              marginTop: 28,
              fontFamily: theme.font.display,
              fontSize: theme.size.statLabel,
              color: theme.color.muted,
              lineHeight: 1.35,
            }}
          >
            {scene.statLabel}
          </div>
        ) : null}
      </div>
    </Stage>
  );
};

const CodeScene: React.FC<{ scene: Scene; terminal: boolean }> = ({ scene, terminal }) => (
  <Stage>
    {scene.title ? (
      <div
        style={{
          fontFamily: theme.font.display,
          fontSize: theme.size.sceneSubtitle,
          color: theme.color.muted,
          marginBottom: 26,
        }}
      >
        {scene.title}
      </div>
    ) : null}
    <CodeBlock
      tokens={scene.tokens ?? []}
      terminal={terminal}
      filename={terminal ? "zsh" : scene.codeLanguage}
    />
  </Stage>
);

const ScreenshotScene: React.FC<{ scene: Scene; repo: RepoMeta }> = ({ scene, repo }) => {
  if (!scene.imageSrc) return null;
  return (
    <Stage>
      {/* The repo name, not the full URL. The link is what the comment-to-DM
          mechanic trades for a follow, so the video should not hand it over in
          the address bar. It names the project plainly instead, which is the
          honest version: anyone determined can search it, and the offer in the
          caption is convenience rather than secrecy. */}
      <BrowserFrame src={scene.imageSrc} url={repo.name} />
    </Stage>
  );
};

export const SceneRenderer: React.FC<{ scene: Scene; repo: RepoMeta }> = ({ scene, repo }) => {
  switch (scene.kind) {
    case "screenshot":
      return <ScreenshotScene scene={scene} repo={repo} />;
    case "repo_card":
      return <RepoCardScene scene={scene} repo={repo} />;
    case "bullets":
      return <BulletsScene scene={scene} />;
    case "stat":
      return <StatScene scene={scene} />;
    case "code":
      return <CodeScene scene={scene} terminal={false} />;
    case "terminal":
      return <CodeScene scene={scene} terminal />;
    default:
      // Unknown kind from a newer Python side: render nothing rather than
      // crashing the whole video.
      return null;
  }
};
