# Slide assets

| File | Slide | Status |
| :-- | :-- | :-- |
| `agent-anatomy.png` | `[anatomy]` | ✅ in |
| `sandbox.png` | `[title]` | ✅ in |
| `production.png` | `[title]` | ✅ in |
| *(tbd)* | `[practice-4]` | ⬜ knowledge-frontier chart |

## Why the title panels are squarish

`sandbox.png` is 612×408 (**3:2 landscape**); `production.png` is 408×612 (**2:3 portrait**).
Opposite orientations, which makes "same size panels" a real constraint rather than a free choice.

Equal **wide** panels would have been the obvious try, and it's wrong: covering a 1.67:1 box
with a 0.67:1 source crops ~60% of the portrait's height, leaving a thin mid-band with the
worker's head cut off.

Equal **squarish** panels (404 × 384) split the loss evenly instead, each image gives up about
a third of its long edge:

```css
.beforeafter {
  grid-template-columns: 404px 404px;
  justify-content: center;      /* centred, so it reads as a deliberate diptych */
}
```

Each crop is then biased toward its subject rather than the geometric centre:

```css
.beforeafter figure:first-child img { object-position: 54% center; }  /* the child */
.beforeafter figure:last-child  img { object-position: center 42%; }  /* head + derrick */
```

**If you swap either image, re-check both numbers.** Different framing needs different values.
For the vertical axis a higher % reveals more of the image's lower part; same for horizontal
and its right side.

## The remaining slot

`[practice-4]`: "Measure, don't guess". Drop the file here, then replace the
`<div class="placeholder">` block with:

```html
<img src="assets/your-chart.png" class="figimg">
```

Then verify layout:

```bash
node ~/.claude/skills/revealjs/scripts/check-overflow.js slides/index.html
```

## ⚠️ Check the licence before this goes in front of a client

Both title images look like commercial stock. Watermark-free preview downloads from paid stock
sites are generally **not** licensed for presentation use, and this is a client deck. Worth
thirty seconds confirming the licence covers it, or swapping for Unsplash / Pexels equivalents,
which are free for commercial use. Search terms that work: "toddler sandpit", "child sandcastle"
and "oil rig worker", "refinery worker hard hat".

## model-frontier.mp4 / .gif / .png

`[practice-4]` plays a **looping, muted, autoplaying screen recording** of the interactive
frontier tool, the timeline sweeps and you watch the frontier march up and to the left, which
is the whole point of the slide. Source was a 2592x1674 / 120fps .mov; encoded to h264 at
1600px / 24fps / crf 26 (221 KB).

`model-frontier.gif` (648 KB) is a fallback inside the `<video>` for any renderer that won't
play mp4, and `model-frontier.png` is the poster frame. A GIF at the source resolution would
have been tens of MB, which is why the video is the primary.

The still chart, kept as the poster: Intelligence index (y) against blended price per
million tokens (x, **doubling** at each gridline). Solid line is the current frontier; the two
dashed lines are where it sat in Jul 25 and Jan 26, that movement up-and-left is the point of
the slide, not any single model's position.

Rendered at full slide width (`max-height:552px; width:100%`) rather than the default `.figimg`
cap, because the label density needs the pixels. It is a light-background asset, so `.figimg`'s
white padding and rounded corners make it read as a deliberate panel.

Worth re-generating before any future delivery, the whole premise of the slide is that this
chart goes stale.
