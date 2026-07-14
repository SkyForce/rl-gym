const pptxgen = require("pptxgenjs");

const p = new pptxgen();
p.layout = "LAYOUT_16x9";          // 10 x 5.625"
p.author = "RL Gym for Agents";
p.title = "RL Gym for Agents";

// ---- palette ----
const DARK = "0F172A";
const TEAL = "0D9488";
const MINT = "5EEAD4";
const AMBER = "F59E0B";
const TEXT = "1E293B";
const MUTED = "64748B";
const WHITE = "FFFFFF";
const CARD = "F1F5F9";
const LINE = "E2E8F0";

const HEAD = "Trebuchet MS";
const BODY = "Calibri";

const W = 10, H = 5.625, M = 0.55;
const mkShadow = () => ({ type: "outer", color: "0F172A", blur: 9, offset: 3, angle: 135, opacity: 0.12 });

function footer(s, n, dark) {
  s.addText("RL Gym for Agents", { x: M, y: H - 0.36, w: 3, h: 0.25, fontFace: BODY,
    fontSize: 9, color: dark ? "94A3B8" : MUTED, align: "left", margin: 0 });
  s.addText(String(n).padStart(2, "0"), { x: W - 1.05, y: H - 0.36, w: 0.5, h: 0.25,
    fontFace: BODY, fontSize: 9, color: dark ? "94A3B8" : MUTED, align: "right", margin: 0 });
}
function header(s, kicker, title) {
  s.addText(kicker.toUpperCase(), { x: M, y: 0.45, w: 9, h: 0.3, fontFace: BODY, bold: true,
    fontSize: 12, color: TEAL, charSpacing: 3, margin: 0 });
  s.addText(title, { x: M, y: 0.74, w: W - 2 * M, h: 0.85, fontFace: HEAD, bold: true,
    fontSize: 30, color: TEXT, margin: 0 });
}
function card(s, x, y, w, h, fill) {
  s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: fill || WHITE },
    line: { color: LINE, width: 1 }, rectRadius: 0.08, shadow: mkShadow() });
}

// ============================================================ 1. TITLE
let s = p.addSlide();
s.background = { color: DARK };
s.addShape(p.shapes.OVAL, { x: 7.7, y: -1.6, w: 4.2, h: 4.2, fill: { color: "14532D", transparency: 55 } });
s.addShape(p.shapes.OVAL, { x: 8.7, y: 3.1, w: 3.0, h: 3.0, fill: { color: TEAL, transparency: 70 } });
s.addText("NEBIUS · SERVERLESS AI BUILDERS CHALLENGE", { x: M, y: 1.0, w: 8.5, h: 0.3,
  fontFace: BODY, bold: true, fontSize: 12, color: MINT, charSpacing: 3, margin: 0 });
s.addText("RL Gym for Agents", { x: M, y: 1.4, w: 9, h: 1.1, fontFace: HEAD, bold: true,
  fontSize: 50, color: WHITE, margin: 0 });
s.addText("Train LLM agents on verifiable rewards.\nShip them serverless.", {
  x: M, y: 2.7, w: 8.6, h: 1.0, fontFace: HEAD, fontSize: 22, color: MINT, lineSpacingMultiple: 1.05, margin: 0 });
s.addText([
  { text: "Flagship example:  ", options: { bold: true, color: "E2E8F0" } },
  { text: "cold-start recommendation agents.", options: { color: "CBD5E1" } },
], { x: M, y: 4.0, w: 8.6, h: 0.4, fontFace: BODY, fontSize: 15, margin: 0 });
footer(s, 1, true);

// ============================================================ 2. THESIS
s = p.addSlide();
s.background = { color: WHITE };
header(s, "Why an RL gym for agents", "An agent is only as good as its reward");
const th = [
  [MUTED, "Human labels don't scale", "Slow, costly, and can never cover the space of states an agent actually meets."],
  [MUTED, "Learned reward models drift", "A model judging a model is fragile — it gets gamed and silently rewards the wrong thing."],
  [TEAL, "Verifiable rewards win", "Check the outcome directly — no labels, no judge. The environment that defines it is the moat."],
];
let cx = M;
const cw = (W - 2 * M - 2 * 0.3) / 3;
th.forEach((t, i) => {
  const hot = i === 2;
  card(s, cx, 1.85, cw, 2.65, hot ? "ECFDF5" : WHITE);
  if (hot) s.addShape(p.shapes.RECTANGLE, { x: cx, y: 1.85, w: 0.12, h: 2.65, fill: { color: TEAL } });
  s.addText(t[1], { x: cx + 0.3, y: 2.12, w: cw - 0.6, h: 0.7, fontFace: HEAD, bold: true,
    fontSize: 17, color: hot ? TEAL : TEXT, margin: 0, lineSpacingMultiple: 1.0 });
  s.addText(t[2], { x: cx + 0.3, y: 2.9, w: cw - 0.6, h: 1.4, fontFace: BODY, fontSize: 13,
    color: hot ? TEXT : MUTED, margin: 0, lineSpacingMultiple: 1.05 });
  cx += cw + 0.3;
});
footer(s, 2, false);

// ============================================================ 3. THE GYM
s = p.addSlide();
s.background = { color: WHITE };
header(s, "The gym", "One skeleton, any agent");
card(s, 3.7, 1.95, 2.6, 0.95, DARK);
s.addText("reward core", { x: 3.7, y: 2.06, w: 2.6, h: 0.4, align: "center", fontFace: HEAD,
  bold: true, fontSize: 16, color: MINT, margin: 0 });
s.addText("verifiable: parse → score", { x: 3.7, y: 2.46, w: 2.6, h: 0.35, align: "center",
  fontFace: BODY, fontSize: 11, color: "CBD5E1", margin: 0 });
card(s, M, 3.5, 4.0, 1.1, CARD);
s.addText("Training face", { x: M + 0.25, y: 3.62, w: 3.5, h: 0.35, fontFace: HEAD, bold: true, fontSize: 14, color: TEAL, margin: 0 });
s.addText("GRPO tunes the agent on this reward", { x: M + 0.25, y: 3.96, w: 3.5, h: 0.5, fontFace: BODY, fontSize: 12, color: MUTED, margin: 0 });
card(s, W - M - 4.0, 3.5, 4.0, 1.1, CARD);
s.addText("Serving / eval face", { x: W - M - 3.75, y: 3.62, w: 3.5, h: 0.35, fontFace: HEAD, bold: true, fontSize: 14, color: TEAL, margin: 0 });
s.addText("Same function scores the live agent", { x: W - M - 3.75, y: 3.96, w: 3.5, h: 0.5, fontFace: BODY, fontSize: 12, color: MUTED, margin: 0 });
s.addShape(p.shapes.LINE, { x: 4.3, y: 2.9, w: -1.6, h: 0.6, line: { color: "94A3B8", width: 1.5, endArrowType: "triangle" } });
s.addShape(p.shapes.LINE, { x: 5.7, y: 2.9, w: 1.6, h: 0.6, line: { color: "94A3B8", width: 1.5, endArrowType: "triangle" } });
s.addText([
  { text: "Retarget any agent by swapping the reward + data.  ", options: { bold: true, color: TEXT } },
  { text: "Environment + reward + GRPO + serving — the rest is plumbing.", options: { color: MUTED } },
], { x: M, y: 4.66, w: W - 2 * M, h: 0.45, align: "center", fontFace: BODY, fontSize: 13.5, margin: 0 });
footer(s, 3, false);

// ============================================================ 4. FLAGSHIP
s = p.addSlide();
s.background = { color: WHITE };
header(s, "Flagship example", "Recommendation agents");
s.addText("Every business has recommendation — and every business has the cold-start problem.", {
  x: M, y: 1.62, w: 9, h: 0.35, fontFace: BODY, fontSize: 14, color: MUTED, margin: 0 });
const ins = [
  [TEAL, "The conversation IS the user model", "The agent reads what you say — “vegetarian, no nuts, 30-min dinners” — and personalizes from word one. No history needed."],
  [AMBER, "Verifiable reward, no labels", "Tune against checkable outcomes — nutrition, budget, real orders. No human labels, no learned reward model."],
];
let ix = M;
const iw = (W - 2 * M - 0.4) / 2;
ins.forEach((it) => {
  card(s, ix, 2.1, iw, 2.45, WHITE);
  s.addShape(p.shapes.RECTANGLE, { x: ix, y: 2.1, w: 0.12, h: 2.45, fill: { color: it[0] } });
  s.addText(it[1], { x: ix + 0.4, y: 2.35, w: iw - 0.7, h: 0.85, fontFace: HEAD, bold: true,
    fontSize: 19, color: TEXT, margin: 0, lineSpacingMultiple: 1.0 });
  s.addText(it[2], { x: ix + 0.4, y: 3.3, w: iw - 0.7, h: 1.1, fontFace: BODY, fontSize: 13.5,
    color: MUTED, margin: 0, lineSpacingMultiple: 1.05 });
  ix += iw + 0.4;
});
footer(s, 4, false);

// ============================================================ 5. HOW IT WORKS
s = p.addSlide();
s.background = { color: WHITE };
header(s, "How the agent works", "LLM in front, ranking in the engine");
const nodes = [
  ["Text request", "“something quick &\nhealthy for 2”"],
  ["LLM understands", "intent + constraints\n+ steering policy"],
  ["Retrieve", "content / catalog\n→ candidates"],
  ["RL re-rank", "compose & order\nfor the objective"],
  ["Reply", "recommendations\n+ why they fit"],
];
let nx = 0.5;
const nw = 1.66, ny = 2.05, gap = 0.21;
nodes.forEach((nd, i) => {
  card(s, nx, ny, nw, 1.35, i === 3 ? "FFFBEB" : CARD);
  s.addText(nd[0], { x: nx + 0.1, y: ny + 0.16, w: nw - 0.2, h: 0.4, align: "center",
    fontFace: HEAD, bold: true, fontSize: 13.5, color: i === 3 ? "B45309" : TEAL, margin: 0 });
  s.addText(nd[1], { x: nx + 0.1, y: ny + 0.6, w: nw - 0.2, h: 0.7, align: "center",
    fontFace: BODY, fontSize: 11, color: MUTED, margin: 0, lineSpacingMultiple: 1.0 });
  if (i < nodes.length - 1)
    s.addText("→", { x: nx + nw - 0.04, y: ny, w: gap + 0.08, h: 1.35, align: "center",
      valign: "middle", fontFace: BODY, bold: true, fontSize: 18, color: "94A3B8", margin: 0 });
  nx += nw + gap;
});
card(s, M, 3.9, W - 2 * M, 0.95, "ECFDF5");
s.addText([
  { text: "The LLM is the interface, not the ranker.  ", options: { bold: true, color: TEAL } },
  { text: "Ranking stays in the engine; the verifiable reward trains and serves it.", options: { color: TEXT } },
], { x: M + 0.3, y: 3.9, w: W - 2 * M - 0.6, h: 0.95, valign: "middle", fontFace: BODY, fontSize: 14, margin: 0 });
footer(s, 5, false);

// ============================================================ 6. WHERE IT WINS
s = p.addSlide();
s.background = { color: WHITE };
header(s, "Be honest", "Where generative + RL actually wins");
const colw = (W - 2 * M - 0.4) / 2;
card(s, M, 1.9, colw, 2.75, CARD);
s.addText("Classical wins here", { x: M + 0.3, y: 2.12, w: colw - 0.6, h: 0.4, fontFace: HEAD,
  bold: true, fontSize: 17, color: MUTED, margin: 0 });
s.addText([
  { text: "Warm, dense, pure-CTR ranking", options: { bullet: true, breakLine: true } },
  { text: "Huge scale, low latency, low cost", options: { bullet: true, breakLine: true } },
  { text: "“Users like you” collaborative lift", options: { bullet: true, breakLine: true } },
  { text: "Battle-tested, low risk", options: { bullet: true } },
], { x: M + 0.3, y: 2.6, w: colw - 0.6, h: 1.9, fontFace: BODY, fontSize: 13.5, color: TEXT,
  margin: 0, paraSpaceAfter: 7 });
card(s, M + colw + 0.4, 1.9, colw, 2.75, "ECFDF5");
s.addShape(p.shapes.RECTANGLE, { x: M + colw + 0.4, y: 1.9, w: 0.12, h: 2.75, fill: { color: TEAL } });
s.addText("RL agents win — two places", { x: M + colw + 0.78, y: 2.12, w: colw - 0.7, h: 0.4,
  fontFace: HEAD, bold: true, fontSize: 17, color: TEAL, margin: 0 });
s.addText([
  { text: "Cold-start: rank from intent & content, not history", options: { bullet: true, breakLine: true } },
  { text: "Composition: build coherent sets (meal plans, outfits)", options: { bullet: true, breakLine: true } },
  { text: "Constraints: optimize nutrition / budget / margin together", options: { bullet: true, breakLine: true } },
  { text: "Steerable in plain English — no retraining", options: { bullet: true } },
], { x: M + colw + 0.78, y: 2.6, w: colw - 1.05, h: 1.9, fontFace: BODY, fontSize: 13.5,
  color: TEXT, margin: 0, paraSpaceAfter: 7 });
footer(s, 6, false);

// ============================================================ 7. FIRST BUILD
s = p.addSlide();
s.background = { color: WHITE };
header(s, "First build", "The meal & grocery agent");
s.addText("Why this is the first agent to ship:", { x: M, y: 1.7, w: 9, h: 0.35,
  fontFace: BODY, fontSize: 14, color: MUTED, margin: 0 });
const reasons = [
  ["Cleanest reward", "Nutrition, budget, dietary rules are calculable — a verifiable reward with no logged data."],
  ["Public data ready", "Instacart baskets + USDA nutrition + Food.com ratings — train a real model, not a toy."],
  ["Cold-start friendly", "Food preferences are declarable: “vegetarian, no nuts, love Thai.” The agent nails it."],
  ["Fast flywheel", "Groceries are bought weekly — outcomes accrue fast, so the agent improves quickly."],
];
let ry = 2.15;
const rw = (W - 2 * M - 0.3) / 2, rh = 1.18;
reasons.forEach((r, i) => {
  const px = M + (i % 2) * (rw + 0.3);
  const py = ry + Math.floor(i / 2) * (rh + 0.22);
  card(s, px, py, rw, rh, CARD);
  s.addText(r[0], { x: px + 0.28, y: py + 0.16, w: rw - 0.5, h: 0.4, fontFace: HEAD, bold: true,
    fontSize: 15, color: TEAL, margin: 0 });
  s.addText(r[1], { x: px + 0.28, y: py + 0.55, w: rw - 0.5, h: 0.6, fontFace: BODY, fontSize: 12.5,
    color: TEXT, margin: 0, lineSpacingMultiple: 1.0 });
});
footer(s, 7, false);

// ============================================================ 8. IN ACTION
s = p.addSlide();
s.background = { color: WHITE };
header(s, "In action", "From one sentence to a week of dinners");
card(s, M, 1.7, W - 2 * M, 0.6, DARK);
s.addText([
  { text: "User:  ", options: { bold: true, color: MINT } },
  { text: "“Vegetarian, no nuts, 30-min dinners, about $60 for two — this week.”", options: { color: WHITE, italic: true } },
], { x: M + 0.25, y: 1.7, w: W - 2 * M - 0.5, h: 0.6, valign: "middle", fontFace: BODY, fontSize: 14, margin: 0 });
const rows = [
  [{ text: "Day", options: { bold: true, color: WHITE, fill: { color: TEAL } } },
   { text: "Dinner (cuisine)", options: { bold: true, color: WHITE, fill: { color: TEAL } } },
   { text: "kcal", options: { bold: true, color: WHITE, fill: { color: TEAL }, align: "center" } },
   { text: "protein", options: { bold: true, color: WHITE, fill: { color: TEAL }, align: "center" } },
   { text: "time", options: { bold: true, color: WHITE, fill: { color: TEAL }, align: "center" } },
   { text: "$/serv", options: { bold: true, color: WHITE, fill: { color: TEAL }, align: "center" } }],
  ["Mon", "Chickpea coconut curry  (Indian)", "540", "22 g", "25 m", "$6.20"],
  ["Tue", "Caprese orzo + white beans  (Italian)", "610", "24 g", "20 m", "$5.10"],
  ["Wed", "Black-bean & sweet-potato tacos  (Mexican)", "580", "18 g", "30 m", "$4.80"],
  ["Thu", "Miso-glazed tofu rice bowl  (Japanese)", "560", "27 g", "25 m", "$5.60"],
];
const styled = rows.map((r, ri) => r.map((c) => {
  if (ri === 0) return c;
  const str = typeof c === "string" ? c : c.text;
  return { text: str, options: { fontSize: 12, color: TEXT, fill: { color: ri % 2 ? WHITE : "F8FAFC" },
    align: (/[\d$]/.test(str) && str.length < 7) ? "center" : "left" } };
}));
s.addTable(styled, { x: M, y: 2.45, w: W - 2 * M, colW: [0.6, 4.35, 0.85, 0.95, 0.8, 1.35],
  rowH: [0.32, 0.36, 0.36, 0.36, 0.36], fontFace: BODY, border: { pt: 0.5, color: LINE }, valign: "middle" });
s.addText("✓ vegetarian   ✓ nut-free   ✓ ≤ 30 min   ✓ $43.40 / $60   ✓ 4 cuisines (variety)", {
  x: M, y: 4.55, w: W - 2 * M, h: 0.4, align: "center", fontFace: BODY, bold: true, fontSize: 13.5, color: TEAL, margin: 0 });
footer(s, 8, false);

// ============================================================ 9. WHERE RL HELPS
s = p.addSlide();
s.background = { color: WHITE };
header(s, "Where RL earns its keep", "Compose the plan, balance the trade-offs");
card(s, M, 1.9, 4.55, 2.75, CARD);
s.addText("The reward stacks up", { x: M + 0.28, y: 2.08, w: 4.0, h: 0.4, fontFace: HEAD, bold: true, fontSize: 16, color: TEXT, margin: 0 });
s.addText([{ text: "Calculable", options: { bold: true, color: TEAL } }, { text: " — nutrition, budget, variety  (no data needed)", options: { color: TEXT } }],
  { x: M + 0.28, y: 2.55, w: 4.0, h: 0.5, fontFace: BODY, fontSize: 13, margin: 0, lineSpacingMultiple: 1.0 });
s.addText([{ text: "Palatability", options: { bold: true, color: TEAL } }, { text: " — public recipe ratings (population taste)", options: { color: TEXT } }],
  { x: M + 0.28, y: 3.18, w: 4.0, h: 0.5, fontFace: BODY, fontSize: 13, margin: 0, lineSpacingMultiple: 1.0 });
s.addText([{ text: "Personal fit", options: { bold: true, color: AMBER } }, { text: " — your reorders & skips (data-gated)", options: { color: TEXT } }],
  { x: M + 0.28, y: 3.81, w: 4.0, h: 0.5, fontFace: BODY, fontSize: 13, margin: 0, lineSpacingMultiple: 1.0 });
card(s, M + 4.85, 1.9, W - 2 * M - 4.85, 2.75, "ECFDF5");
s.addShape(p.shapes.RECTANGLE, { x: M + 4.85, y: 1.9, w: 0.12, h: 2.75, fill: { color: AMBER } });
s.addText("RL optimizes the whole plan", { x: M + 5.2, y: 2.08, w: 3.6, h: 0.4, fontFace: HEAD, bold: true, fontSize: 16, color: "B45309", margin: 0 });
s.addText([
  { text: "Listwise: a coherent week, not 7 top picks", options: { bullet: true, breakLine: true } },
  { text: "Trades nutrition ⊗ budget ⊗ variety ⊗ taste", options: { bullet: true, breakLine: true } },
  { text: "Learns when to bend — no static rule can", options: { bullet: true, breakLine: true } },
  { text: "Bootstraps on public data, then real orders", options: { bullet: true } },
], { x: M + 5.2, y: 2.55, w: 3.5, h: 2.0, fontFace: BODY, fontSize: 13, color: TEXT, margin: 0, paraSpaceAfter: 7 });
s.addText("Taste is learned by SFT · RL optimizes the policy · hard filters (allergens) stay deterministic", {
  x: M, y: 4.78, w: W - 2 * M, h: 0.4, align: "center", fontFace: BODY, italic: true, fontSize: 11.5, color: MUTED, margin: 0 });
footer(s, 9, false);

// ============================================================ 10. MORE VERTICALS
s = p.addSlide();
s.background = { color: WHITE };
header(s, "Beyond the flagship", "More agents, same gym");
const verts = [
  ["Fashion", "Compose outfits from a text vibe", "reward: compatibility + purchases / returns"],
  ["Tourism", "Plan multi-day itineraries", "reward: budget + time + geo / hours feasibility"],
  ["Retail (per-brand)", "On-site discovery for one catalog", "reward: conversion + margin + stock"],
  ["Meals & grocery", "Weekly plans & baskets", "reward: nutrition + budget + dietary (calculable)"],
  ["Data / SQL", "Answer questions over a warehouse", "reward: query executes & matches (verified)"],
  ["Tool-use", "Multi-step task completion", "reward: goal state reached"],
];
const gw = (W - 2 * M - 2 * 0.25) / 3, gh = 1.4;
verts.forEach((v, i) => {
  const gx = M + (i % 3) * (gw + 0.25);
  const gy = 1.8 + Math.floor(i / 3) * (gh + 0.2);
  const flag = i === 3; // meals = flagship-adjacent, highlight
  card(s, gx, gy, gw, gh, flag ? "ECFDF5" : WHITE);
  s.addText(v[0], { x: gx + 0.22, y: gy + 0.16, w: gw - 0.44, h: 0.35, fontFace: HEAD, bold: true,
    fontSize: 15, color: TEAL, margin: 0 });
  s.addText(v[1], { x: gx + 0.22, y: gy + 0.54, w: gw - 0.44, h: 0.5, fontFace: BODY, fontSize: 12,
    color: TEXT, margin: 0, lineSpacingMultiple: 1.0 });
  s.addText(v[2], { x: gx + 0.22, y: gy + 1.0, w: gw - 0.44, h: 0.35, fontFace: BODY, italic: true,
    fontSize: 10.5, color: MUTED, margin: 0, lineSpacingMultiple: 1.0 });
});
footer(s, 10, false);

// ============================================================ 11. VISION + ROADMAP + CTA
s = p.addSlide();
s.background = { color: DARK };
s.addText("VISION · ROADMAP", { x: M, y: 0.55, w: 9, h: 0.3, fontFace: BODY, bold: true, fontSize: 12, color: MINT, charSpacing: 3, margin: 0 });
s.addText("Any agent with a verifiable reward —\nship cold, then let the data compound", { x: M, y: 0.9, w: 9, h: 1.0,
  fontFace: HEAD, bold: true, fontSize: 25, color: WHITE, margin: 0, lineSpacingMultiple: 1.0 });
const phases = [
  ["Phase 1", "Ship cold", "LLM intent + content retrieval + a calculable reward. Runs on public data — no logs."],
  ["Phase 2", "Learn taste", "SFT on reorders + RL composite reward on real outcomes. The flywheel turns."],
  ["Phase 3", "Scale & adapt", "Collaborative lift + real-time exploration, once the user base is dense."],
];
let px2 = M;
const pw = (W - 2 * M - 2 * 0.3) / 3;
phases.forEach((ph, i) => {
  s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: px2, y: 2.1, w: pw, h: 1.65, fill: { color: "1E293B" },
    line: { color: i === 0 ? TEAL : "334155", width: i === 0 ? 1.5 : 1 }, rectRadius: 0.08 });
  s.addText(ph[0], { x: px2 + 0.25, y: 2.26, w: pw - 0.5, h: 0.3, fontFace: BODY, bold: true, fontSize: 11, color: AMBER, charSpacing: 2, margin: 0 });
  s.addText(ph[1], { x: px2 + 0.25, y: 2.55, w: pw - 0.5, h: 0.4, fontFace: HEAD, bold: true, fontSize: 17, color: WHITE, margin: 0 });
  s.addText(ph[2], { x: px2 + 0.25, y: 2.97, w: pw - 0.5, h: 0.72, fontFace: BODY, fontSize: 11.5, color: "CBD5E1", margin: 0, lineSpacingMultiple: 1.0 });
  px2 += pw + 0.3;
});
card(s, M, 4.05, W - 2 * M, 0.9, "0B3B36");
s.addText([
  { text: "Built for Nebius serverless  ", options: { bold: true, color: MINT } },
  { text: "— open-model inference for the agent, H100 RL training for the policy.   ", options: { color: "E2E8F0" } },
  { text: "Fork it, point it at your vertical.", options: { bold: true, color: WHITE } },
], { x: M + 0.3, y: 4.05, w: W - 2 * M - 0.6, h: 0.9, valign: "middle", fontFace: BODY, fontSize: 13.5, margin: 0, lineSpacingMultiple: 1.0 });
footer(s, 11, true);

p.writeFile({ fileName: "RLGymForAgents.pptx" }).then((f) => console.log("wrote", f));
