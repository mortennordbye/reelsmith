/**
 * Shiki syntax highlighting, run once in calculateMetadata.
 *
 * Why here and not in a component: calculateMetadata runs in Node before any
 * frame is rendered, so the WASM highlighter is loaded exactly once per render
 * instead of once per frame in a headless browser. The components receive
 * plain token arrays and stay synchronous.
 */

import { createHighlighter, type Highlighter } from "shiki";

import { SHIKI_THEME } from "./theme";
import type { CodeToken, Scene } from "./types";

// Loading a language costs real time, so we preload the ones this niche
// actually produces and fall back to plaintext for anything else.
const LANGS = [
  "bash", "shell", "python", "typescript", "javascript", "tsx", "jsx",
  "rust", "go", "json", "yaml", "sql", "docker", "toml", "html", "css",
] as const;

const ALIASES: Record<string, string> = {
  sh: "bash",
  zsh: "bash",
  console: "bash",
  shellscript: "bash",
  py: "python",
  ts: "typescript",
  js: "javascript",
  golang: "go",
  yml: "yaml",
  dockerfile: "docker",
  rs: "rust",
};

let highlighterPromise: Promise<Highlighter> | null = null;

function getHighlighter(): Promise<Highlighter> {
  highlighterPromise ??= createHighlighter({
    themes: [SHIKI_THEME],
    langs: [...LANGS],
  });
  return highlighterPromise;
}

function normaliseLang(lang: string | null | undefined): string {
  if (!lang) return "bash";
  const lower = lang.toLowerCase().trim();
  const resolved = ALIASES[lower] ?? lower;
  return (LANGS as readonly string[]).includes(resolved) ? resolved : "text";
}

export async function highlightScenes(scenes: Scene[]): Promise<Scene[]> {
  const needsHighlighting = scenes.some((s) => Boolean(s.code));
  if (!needsHighlighting) return scenes;

  const highlighter = await getHighlighter();

  return scenes.map((scene) => {
    if (!scene.code) return scene;

    const lang = normaliseLang(scene.codeLanguage);
    try {
      const { tokens } = highlighter.codeToTokens(scene.code, {
        lang: lang as never,
        theme: SHIKI_THEME,
      });
      const simplified: CodeToken[][] = tokens.map((line) =>
        line.map((t) => ({ content: t.content, color: t.color ?? "#E6EDF3" })),
      );
      return { ...scene, tokens: simplified };
    } catch {
      // A bad language name must not fail the whole render -- fall back to
      // unhighlighted monospace, which still looks fine.
      const plain: CodeToken[][] = scene.code
        .split("\n")
        .map((line) => [{ content: line, color: "#E6EDF3" }]);
      return { ...scene, tokens: plain };
    }
  });
}
