// Renders assets/banner.png (the README banner) in the videocompression.app visual language.
// Needs the `sharp` package. Point SHARP_FROM at any package.json whose node_modules has sharp, for example
//   SHARP_FROM="/path/to/a/project/package.json" node tools/make_banner.mjs
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(process.env.SHARP_FROM ?? import.meta.url);
const sharp = require("sharp");

const WIDTH = 1600;
const HEIGHT = 520;
const FONT = "'SF Pro Display', 'SF Pro', 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif";
const COLOR = { canvas: "#07101d", ink: "#f6f9ff", soft: "#dce6f5", muted: "#aebdd2", cyan: "#39c6ff", blue: "#477bff", indigo: "#9666ff", action: "#0a84ff" };

const icon = { size: 240, x: 104, y: (HEIGHT - 240) / 2 - 6 };
const radius = icon.size * 0.2237;
const textX = 420;

const text = (value, { x, y, size, weight = 700, fill = COLOR.ink, anchor = "start", spacing = 0 }) =>
  `<text x="${x}" y="${y}" font-family="${FONT}" font-size="${size}" font-weight="${weight}" fill="${fill}" text-anchor="${anchor}" letter-spacing="${spacing}">${value}</text>`;

const chip = (label, x, y) => {
  const width = label.length * 13.5 + 44;
  return {
    width,
    svg: `<rect x="${x}" y="${y}" width="${width}" height="48" rx="24" fill="#ffffff" fill-opacity="0.06" stroke="#cde2ff" stroke-opacity="0.22"/>
      ${text(label, { x: x + width / 2, y: y + 32, size: 23, weight: 600, fill: COLOR.soft, anchor: "middle" })}`
  };
};

let chipX = textX;
const chips = ["Claude Code", "Cursor", "Codex"].map((label) => {
  const c = chip(label, chipX, 372);
  chipX += c.width + 14;
  return c.svg;
});

const ctaLabel = "videocompression.app";
const ctaWidth = 470;
const ctaX = WIDTH - 104 - ctaWidth;

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}">
  <defs>
    <radialGradient id="indigoGlow" cx="0.9" cy="0" r="0.7"><stop stop-color="#6656ff" stop-opacity="0.5"/><stop offset="1" stop-color="#6656ff" stop-opacity="0"/></radialGradient>
    <radialGradient id="cyanGlow" cx="0" cy="1" r="0.6"><stop stop-color="#00afff" stop-opacity="0.28"/><stop offset="1" stop-color="#00afff" stop-opacity="0"/></radialGradient>
    <radialGradient id="iconHalo" cx="0.5" cy="0.5" r="0.5"><stop stop-color="#7a7bff" stop-opacity="0.55"/><stop offset="0.55" stop-color="#3b8bff" stop-opacity="0.16"/><stop offset="1" stop-color="#3b8bff" stop-opacity="0"/></radialGradient>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="0"><stop stop-color="${COLOR.cyan}"/><stop offset="0.52" stop-color="${COLOR.blue}"/><stop offset="1" stop-color="${COLOR.indigo}"/></linearGradient>
    <filter id="iconShadow" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="${icon.size * 0.085}"/></filter>
  </defs>
  <rect width="${WIDTH}" height="${HEIGHT}" fill="${COLOR.canvas}"/>
  <rect width="${WIDTH}" height="${HEIGHT}" fill="url(#indigoGlow)"/>
  <rect width="${WIDTH}" height="${HEIGHT}" fill="url(#cyanGlow)"/>
  <circle cx="${icon.x + icon.size / 2}" cy="${icon.y + icon.size / 2}" r="${icon.size * 1.2}" fill="url(#iconHalo)"/>
  <rect x="${icon.x}" y="${icon.y + icon.size * 0.07}" width="${icon.size}" height="${icon.size}" rx="${radius}" fill="#000" fill-opacity="0.55" filter="url(#iconShadow)"/>
  ${text("AGENT SKILL", { x: textX, y: 118, size: 24, weight: 700, fill: COLOR.cyan, spacing: 4 })}
  ${text("Compress video", { x: textX, y: 214, size: 92 })}
  ${text("from your agent.", { x: textX, y: 308, size: 92 })}
  ${chips.join("\n  ")}
  <rect x="${ctaX}" y="${HEIGHT - 162}" width="${ctaWidth}" height="76" rx="38" fill="${COLOR.action}"/>
  ${text("Also on iPhone and iPad", { x: ctaX + ctaWidth / 2, y: HEIGHT - 130, size: 20, weight: 600, fill: "#ffffff", anchor: "middle" }).replace('fill="#ffffff"', 'fill="#ffffff" fill-opacity="0.82"')}
  ${text(ctaLabel, { x: ctaX + ctaWidth / 2, y: HEIGHT - 102, size: 32, weight: 700, fill: "#ffffff", anchor: "middle" })}
  <rect y="${HEIGHT - 10}" width="${WIDTH}" height="10" fill="url(#brand)"/>
</svg>`;

const iconPng = await sharp(join(root, "skills/video-compression/assets/logo.png")).resize(icon.size, icon.size).png().toBuffer();
await sharp(Buffer.from(svg))
  .composite([{ input: iconPng, left: icon.x, top: Math.round(icon.y) }])
  .png({ compressionLevel: 9 })
  .toFile(join(root, "assets/banner.png"));
console.log("wrote assets/banner.png");
