import { Config } from "@remotion/cli/config";

/**
 * Quality settings.
 *
 * The big one is the image format. Remotion's default is JPEG, which
 * compresses every frame *before* H.264 ever sees it -- so you bake artifacts
 * into the source and then encode the artifacts. On flat dark UI with fine
 * text and syntax highlighting that shows up immediately as mush around glyph
 * edges. PNG frames are lossless and cost only render time.
 */
Config.setVideoImageFormat("png");
Config.setOverwriteOutput(true);
Config.setCodec("h264");

// PNG frames are full-range RGB; this forces correct limited-range tagging so
// players don't shift the blacks. (JPEG frames come out tagged yuvj420p, which
// some players render washed out.)
Config.setPixelFormat("yuv420p");

// Instagram re-encodes on upload, so the only thing that survives their
// pipeline is a clean, high-bitrate source. 14 is near-transparent on the fine
// text and screenshot detail this content is made of.
Config.setCrf(14);

// Slower preset, better compression at the same CRF. Worth it for a ~30s video
// rendered once.
Config.setX264Preset("slow");
