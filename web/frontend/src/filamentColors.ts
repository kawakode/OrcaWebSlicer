import type { Vector } from "./geometry";

/**
 * Colors assigned to filament slots by index (0-based). Shared by the plate
 * canvas — which shades each object's triangles with these RGB triples — and
 * the on-screen filament and object lists, which render the same values as
 * CSS, so the two always agree on which color means which filament.
 *
 * Slot 0 is the plate's long-standing default blue, so a plate with a single
 * filament slot looks exactly as it always has.
 *
 * Colorblind-safe (Okabe-Ito), extended with one more hue for an 8th slot.
 * The API allows up to 16 slots; beyond the palette's own length colors
 * cycle rather than repeat a filament's exact neighbour, so even the last
 * few slots stay visually distinct from the ones right before them.
 */
const FILAMENT_PALETTE: readonly Vector[] = [
  [77, 139, 224], // blue - the plate's original single-object color
  [230, 159, 0], // orange
  [0, 158, 115], // green
  [204, 121, 167], // purple
  [213, 94, 0], // vermillion
  [86, 180, 233], // sky blue
  [220, 191, 26], // yellow
  [136, 136, 136], // grey
];

export function filamentColor(index: number): Vector {
  const length = FILAMENT_PALETTE.length;
  return FILAMENT_PALETTE[((index % length) + length) % length];
}

export function filamentCss(index: number): string {
  const [r, g, b] = filamentColor(index);
  return `rgb(${r}, ${g}, ${b})`;
}
