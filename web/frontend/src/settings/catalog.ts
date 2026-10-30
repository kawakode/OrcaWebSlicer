/**
 * Which catalog settings a form shows. Kept free of rendering so the mode,
 * level, and search rules can be tested on their own; every rule reads the
 * engine's own metadata rather than a list kept here.
 */

import type { SettingDefinition, SettingLevel, SettingMode, SettingsCatalog } from "../types";

const MODE_RANK: Record<SettingMode, number> = { simple: 0, advanced: 1, expert: 2 };

export const MODES: { value: SettingMode; label: string }[] = [
  { value: "simple", label: "Simple" },
  { value: "advanced", label: "Advanced" },
  { value: "expert", label: "Expert" },
];

/** The overrides a user actually typed: blank controls keep the inherited value. */
export function declared(overrides: Record<string, string> | undefined): Record<string, string> {
  return Object.fromEntries(Object.entries(overrides ?? {}).filter(([, value]) => value.trim() !== ""));
}

export function appliesAt(setting: SettingDefinition, level: SettingLevel): boolean {
  return level === "plate" || (level === "object" ? setting.per_object : setting.per_range);
}

/** Case-insensitive match on what a user would type: the label, the key, or the group. */
export function matches(setting: SettingDefinition, groupLabel: string, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const haystack = `${setting.label} ${setting.key} ${groupLabel}`.toLowerCase();
  return words.every((word) => haystack.includes(word));
}

/**
 * The settings to show, grouped as the catalog groups them. A setting with an
 * override is always shown, so a mode switch never hides a value that is sent.
 */
export function visibleGroups(
  catalog: SettingsCatalog,
  options: { mode: SettingMode; level: SettingLevel; query: string; overrides: Record<string, string> },
) {
  const { mode, level, query, overrides } = options;
  return catalog.groups
    .map((group) => ({
      ...group,
      settings: catalog.settings.filter(
        (setting) =>
          !setting.missing &&
          setting.group === group.id &&
          appliesAt(setting, level) &&
          (overrides[setting.key]?.trim() || MODE_RANK[setting.mode] <= MODE_RANK[mode]) &&
          matches(setting, group.label, query),
      ),
    }))
    .filter((group) => group.settings.length > 0);
}
