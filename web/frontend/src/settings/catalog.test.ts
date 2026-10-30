import { describe, expect, it } from "vitest";

import type { SettingDefinition, SettingsCatalog } from "../types";
import { declared, visibleGroups } from "./catalog";

function setting(key: string, group: string, extra: Partial<SettingDefinition> = {}): SettingDefinition {
  return { key, group, scope: "process", type: "float", vector: false, mode: "simple", label: key, per_object: true, per_range: true, ...extra };
}

const CATALOG: SettingsCatalog = {
  catalog_version: 2,
  engine_version: "test",
  groups: [
    { id: "quality", label: "Quality" },
    { id: "support", label: "Support" },
  ],
  settings: [
    setting("layer_height", "quality", { label: "Layer height" }),
    setting("wall_loops", "quality", { label: "Wall loops", mode: "advanced" }),
    setting("hole_to_polyhole", "quality", { label: "Convert holes to polyholes", mode: "expert" }),
    setting("skirt_loops", "quality", { label: "Skirt loops", per_object: false, per_range: false }),
    setting("enable_support", "support", { label: "Enable support", per_range: false }),
  ],
};

const keys = (groups: ReturnType<typeof visibleGroups>) => groups.flatMap((group) => group.settings.map((s) => s.key));

describe("settings visibility", () => {
  it("shows more settings in each higher mode", () => {
    const shown = (mode: "simple" | "advanced" | "expert") =>
      keys(visibleGroups(CATALOG, { mode, level: "plate", query: "", overrides: {} }));
    expect(shown("simple")).toEqual(["layer_height", "skirt_loops", "enable_support"]);
    expect(shown("advanced")).toContain("wall_loops");
    expect(shown("advanced")).not.toContain("hole_to_polyhole");
    expect(shown("expert")).toHaveLength(5);
  });

  it("keeps an overridden setting visible whatever the mode", () => {
    const groups = visibleGroups(CATALOG, { mode: "simple", level: "plate", query: "", overrides: { wall_loops: "4" } });
    expect(keys(groups)).toContain("wall_loops");
  });

  it("offers an object or a height range only what the engine applies there", () => {
    const at = (level: "object" | "range") => keys(visibleGroups(CATALOG, { mode: "expert", level, query: "", overrides: {} }));
    expect(at("object")).not.toContain("skirt_loops");
    expect(at("object")).toContain("enable_support");
    expect(at("range")).not.toContain("enable_support");
  });

  it("searches labels, keys, and group names, dropping empty groups", () => {
    const search = (query: string) => visibleGroups(CATALOG, { mode: "expert", level: "plate", query, overrides: {} });
    expect(keys(search("LAYER"))).toEqual(["layer_height"]);
    expect(keys(search("polyhole"))).toEqual(["hole_to_polyhole"]);
    expect(search("support").map((group) => group.id)).toEqual(["support"]);
    expect(keys(search("wall loops"))).toEqual(["wall_loops"]);
    expect(search("nothing-matches")).toEqual([]);
  });

  it("sends only the overrides a user typed", () => {
    expect(declared({ layer_height: "0.1", wall_loops: " ", support: "" })).toEqual({ layer_height: "0.1" });
  });
});
