---
name: remotion-math-video
description: "Create math explanation videos using Remotion + KaTeX + SVG. Use when the user asks for a math video, tutorial video, animated math explanation, or any video that needs mathematical formulas and animated graphs. Covers project setup, KaTeX rendering, SVG coordinate mapping, animation patterns, and rendering to MP4."
---

# Remotion Math Video Skill

Generate math explanation videos as MP4 files using Remotion (React-based video framework), KaTeX (math formulas), and SVG (animated graphs).

## Project Structure

```
project/
├── src/
│   ├── index.ts              # Entry point (registerRoot)
│   ├── Root.tsx               # Composition definition
│   ├── Video.tsx              # Main video component (sequences)
│   ├── components/
│   │   └── Math.tsx           # KaTeX helper function
│   └── scenes/
│       ├── TitleScene.tsx
│       ├── FormulaScene.tsx
│       ├── GraphScene.tsx
│       └── SummaryScene.tsx
├── tsconfig.json
├── package.json
└── remotion.config.ts
```

## Setup

```bash
mkdir project && cd project && npm init -y
npm install remotion @remotion/cli @remotion/renderer react react-dom katex typescript @types/react @types/react-dom
```

**tsconfig.json**:
```json
{"compilerOptions":{"target":"ES2022","module":"ES2022","moduleResolution":"bundler","jsx":"react-jsx","strict":true,"skipLibCheck":true,"esModuleInterop":true},"include":["src/**/*.ts","src/**/*.tsx"]}
```

**remotion.config.ts**: `import { Config } from "@remotion/cli/config"; Config.setEntryPoint("./src/index.ts");`

**src/index.ts**: `import { registerRoot } from "remotion"; import { RemotionRoot } from "./Root"; registerRoot(RemotionRoot);`

## KaTeX Integration

**DO NOT** name a React component `Math` — shadows global `Math` object → `Math.sin is not a function`.

Use a helper function:
```ts
// src/components/Math.tsx
import katex from "katex";
export function mathHTML(tex: string): string {
  return katex.renderToString(tex, { throwOnError: false, displayMode: true });
}
```

Usage:
```tsx
import { mathHTML } from "../components/Math";
<div style={{ fontSize: 48, color: "#93c5fd" }} dangerouslySetInnerHTML={{ __html: mathHTML("E = mc^2") }} />
```

### Common Formulas

```
\frac{a}{b}  \sqrt{x}  x^{2}  x_{i}  \int_{a}^{b}  \sum_{i=1}^{n}
\lim_{x \to 0}  \vec{v}  \partial  \nabla  \infty  \implies
\text{中文}  \quad
```

## SVG Graph Rendering

### Core: Coordinate Mapping Function

**Always use a `toSVG` function.** Never calculate SVG coordinates directly from math values.

```tsx
const W = 560, H = 380;                    // SVG canvas
const padL = 60, padR = 20, padT = 20, padB = 50;  // padding
const plotW = W - padL - padR;
const plotH = H - padT - padB;

const xMin = 0, xMax = 2 * Math.PI;       // math domain
const yMin = -1.3, yMax = 1.3;             // math range

const toSVG = (x: number, y: number): [number, number] => [
  padL + ((x - xMin) / (xMax - xMin)) * plotW,
  padT + ((yMax - y) / (yMax - yMin)) * plotH,  // y flipped (SVG y goes down)
];
```

**All elements use `toSVG`**: axes, curves, labels, lines, points.

### Coordinate Verification Checklist (CRITICAL)

Before rendering, verify by substituting boundary values:

```tsx
// 1. Check domain boundaries land within SVG canvas
console.log("xMin,yMin →", toSVG(xMin, yMin));  // should be ~(padL, H-padB)
console.log("xMax,yMax →", toSVG(xMax, yMax));  // should be ~(W-padR, padT)

// 2. Check origin (if in range)
console.log("origin →", toSVG(0, 0));  // should be within plot area

// 3. Check that curve points fall within viewBox
const testPoint = toSVG(Math.PI, Math.sin(Math.PI));
console.log(`0 <= ${testPoint[0]} <= ${W}`);  // x in [0, W]
console.log(`0 <= ${testPoint[1]} <= ${H}`);  // y in [0, H]
```

**If any point falls outside [0, W] or [0, H]**: the element will be clipped or invisible. Fix the mapping before proceeding.

### Curve Drawing

```tsx
const steps = 120;
const curvePoints: [number, number][] = [];
for (let i = 0; i <= steps; i++) {
  const x = xMin + (xMax - xMin) * (i / steps);
  if (i / steps <= drawProgress) {
    curvePoints.push(toSVG(x, f(x)));
  }
}
<polyline points={curvePoints.map(([x, y]) => `${x},${y}`).join(" ")} fill="none" stroke="#3b82f6" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" />
```

### Axes Drawing

```tsx
// x axis
<line x1={padL} y1={toSVG(0,0)[1]} x2={W-padR} y2={toSVG(0,0)[1]} stroke="#64748b" strokeWidth={2} />
// y axis
<line x1={padL} y1={padT} x2={padL} y2={H-padB} stroke="#64748b" strokeWidth={2} />
// ticks
{xTicks.map((x, i) => {
  const [sx, sy] = toSVG(x, 0);
  return <g key={i}><line x1={sx} y1={sy-4} x2={sx} y2={sy+4} stroke="#94a3b8" strokeWidth={1.5} /><text x={sx} y={sy+20} fill="#94a3b8" fontSize={12} textAnchor="middle">{labels[i]}</text></g>;
})}
```

## Animation Patterns

### Scene Timing
```tsx
<Composition id="Video" component={Video} durationInFrames={1800} fps={30} width={1920} height={1080} />
// In Video.tsx:
<Sequence from={0} durationInFrames={120}><TitleScene /></Sequence>
<Sequence from={120} durationInFrames={180}><ContentScene /></Sequence>
```

### Fade In/Out
```tsx
const frame = useCurrentFrame();
const fadeIn = interpolate(frame, [0, 20], [0, 1], { extrapolateRight: "clamp" });
const fadeOut = interpolate(frame, [160, 180], [1, 0], { extrapolateRight: "clamp" });
// style={{ opacity: fadeIn * fadeOut }}
```

### Draw Progress
```tsx
const drawProgress = interpolate(frame, [30, 200], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.inOut(Easing.cubic) });
```

### Staggered Items
```tsx
const opacity = interpolate(frame, [item.delay, item.delay + 20], [0, 1], { extrapolateRight: "clamp" });
const x = interpolate(frame, [item.delay, item.delay + 20], [-30, 0], { extrapolateRight: "clamp", easing: Easing.out(Easing.cubic) });
```

## Color Scheme (Dark)

| Use | Hex |
|-----|-----|
| Background | `#0f172a` |
| Primary text | `#f1f5f9` |
| Secondary text | `#94a3b8` |
| Formula/emphasis | `#93c5fd` |
| Success/result | `#6ee7b7` |
| Warning | `#fbbf24` |
| Error | `#ef4444` |
| Purple/aux | `#c4b5fd` |

## Rendering

```bash
npx remotion render CompositionId out/video.mp4
```

- First render downloads Chrome Headless Shell (~113MB)
- ~30-50 fps rendering speed
- Output: MP4 (H.264), default 1920x1080

## Common Pitfalls

1. **`Math.sin is not a function`**: Component named `Math` shadows global. Use `mathHTML` function.
2. **Curve outside coordinate system**: No unified coordinate mapping. Use single `toSVG` for ALL elements.
3. **Verify before rendering**: Substitute boundary values into `toSVG()`, check results fall within SVG viewBox.
4. **React error #130**: Component imported as null. Check export/import.
5. **Missing tsconfig.json**: Remotion requires it.
