import { cp, mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outputDir = resolve(projectDir, "dist");

// Publish only the static frontend and the one image it references. Keeping
// this list explicit prevents local downloads, cookies, scripts, and designs
// from entering the Cloudflare Pages upload.
await rm(outputDir, { recursive: true, force: true });
await mkdir(resolve(outputDir, "designs/backgrounds"), { recursive: true });
await cp(resolve(projectDir, "index.html"), resolve(outputDir, "index.html"));
await cp(
  resolve(projectDir, "designs/backgrounds/music-waves.png"),
  resolve(outputDir, "designs/backgrounds/music-waves.png"),
);

console.log("Built static Sites frontend in dist/");
