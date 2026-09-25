// Render figure sources (JSX modules returning an <svg>) to standalone,
// theme-aware SVG files that follow the reader's light or dark mode.
// Usage: bun tools/docs/render-figures.js <src-dir> <out-dir>
// Example: bun tools/docs/render-figures.js docs/product/figures/src docs/product/figures
import { readdirSync, readFileSync, writeFileSync, mkdirSync, unlinkSync } from "fs";
import { join, basename, resolve } from "path";

const [inDir, outDir] = process.argv.slice(2).map((p) => resolve(p));
mkdirSync(outDir, { recursive: true });

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const kebab = (k) => (/^(viewBox|refX|refY|markerWidth|markerHeight)$/.test(k) ? k : k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()));
const flat = (a) => a.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false && c !== true);

globalThis.h = (tag, props, ...children) => {
  if (typeof tag === "function") return tag({ ...(props || {}), children });
  const attrs = [];
  const styles = [];
  for (const [k, v] of Object.entries(props || {})) {
    if (k.startsWith("data-claude") || v === undefined || v === null) continue;
    const name = kebab(k);
    // CSS variables are not reliable in presentation attributes; move them into style.
    if (typeof v === "string" && v.includes("var(")) styles.push(`${name}:${v}`);
    else attrs.push(`${name}="${esc(v)}"`);
  }
  if (styles.length) attrs.push(`style="${styles.join(";")}"`);
  const inner = flat(children).map((c) => (typeof c === "string" && !c.startsWith("<") ? esc(c) : String(c))).join("");
  return `<${tag}${attrs.length ? " " + attrs.join(" ") : ""}>${inner}</${tag}>`;
};
globalThis.F = (p) => flat(p.children).join("");

const light = {
  "text-primary": "#111827", "text-secondary": "#4b5563", "chart-axis": "#6b7280", "chart-grid": "#e5e7eb",
  "chart-reference-tint": "#f3f4f6", bg: "#ffffff",
  "chart-categorical-1": "#2563eb", "chart-categorical-2": "#0d9488", "chart-categorical-3": "#d97706",
  "chart-categorical-4": "#9333ea", "chart-categorical-5": "#db2777", "chart-categorical-6": "#64748b",
  "chart-categorical-7": "#16a34a", "chart-categorical-8": "#78716c",
};
const dark = {
  "text-primary": "#e5e7eb", "text-secondary": "#9ca3af", "chart-axis": "#9ca3af", "chart-grid": "#374151",
  "chart-reference-tint": "#1f2937", bg: "#0d1117",
  "chart-categorical-1": "#60a5fa", "chart-categorical-2": "#2dd4bf", "chart-categorical-3": "#fbbf24",
  "chart-categorical-4": "#c084fc", "chart-categorical-5": "#f472b6", "chart-categorical-6": "#94a3b8",
  "chart-categorical-7": "#4ade80", "chart-categorical-8": "#a8a29e",
};
const vars = (t) => Object.entries(t).map(([k, v]) => `--cds-${k}:${v}`).join(";");
const style = `<style>svg{${vars(light)};font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}@media (prefers-color-scheme:dark){svg{${vars(dark)}}}</style>`;

const transpiler = new Bun.Transpiler({
  loader: "jsx",
  tsconfig: { compilerOptions: { jsx: "react", jsxFactory: "h", jsxFragmentFactory: "F" } },
});

for (const f of readdirSync(inDir).filter((f) => f.endsWith(".jsx")).sort()) {
  const js = transpiler.transformSync(readFileSync(join(inDir, f), "utf8"));
  const tmp = join(outDir, basename(f, ".jsx") + ".tmp.mjs");
  writeFileSync(tmp, js);
  const mod = await import(tmp);
  unlinkSync(tmp);
  let svg = mod.default();
  const vb = svg.match(/viewBox="0 0 (\d+) (\d+)"/);
  const [w, hgt] = [vb[1], vb[2]];
  svg = svg.replace(/^<svg /, `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${hgt}" `);
  svg = svg.replace(/^(<svg[^>]*>)/, `$1${style}<rect width="100%" height="100%" rx="12" style="fill:var(--cds-bg)"/>`);
  const out = join(outDir, basename(f, ".jsx") + ".svg");
  writeFileSync(out, svg + "\n");
  console.log("wrote", out, svg.length, "bytes");
}
