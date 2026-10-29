import { useState } from "react";

import type { ProfileValue, SettingDefinition, SettingLevel, SettingMode, SettingsCatalog } from "../types";
import { visibleGroups } from "./catalog";

const NUMERIC_TYPES = new Set(["float", "floats", "int", "ints"]);
const BOOLEAN_TYPES = new Set(["bool", "bools"]);
const ENUM_TYPES = new Set(["enum", "enums"]);
const FALSE_LITERALS = new Set(["0", "false", "no", ""]);

/** The value a vector option has for the single extruder the MVP drives. */
export function scalar(value: ProfileValue | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function isOn(value: string | undefined): boolean {
  return value !== undefined && !FALSE_LITERALS.has(value.toLowerCase());
}

/**
 * Renders the catalog settings that apply at `level` as override controls,
 * narrowed by mode and by a search box. Nothing about a setting's type, unit,
 * range, choices, mode, or dependency is stated here: it all comes from the
 * engine's catalog, and the inherited values are only shown as what a blank
 * override keeps.
 */
export function SettingsForm(props: {
  catalog: SettingsCatalog;
  /** What a blank override keeps: the profile chain, plus the plate's overrides for an object. */
  profileValues: Record<string, ProfileValue>;
  overrides: Record<string, string>;
  mode: SettingMode;
  level?: SettingLevel;
  /** Distinguishes the controls of several forms on one page. */
  idPrefix?: string;
  /** The setting the last refusal named, marked invalid until it is edited. */
  invalidKey?: string;
  onChange: (key: string, value: string) => void;
}) {
  const { catalog, profileValues, overrides, mode, level = "plate", idPrefix = "", invalidKey, onChange } = props;
  const [query, setQuery] = useState("");
  // Only the first group starts open; searching opens every match.
  const [opened, setOpened] = useState<Set<string>>(() => new Set(catalog.groups.slice(0, 1).map((group) => group.id)));
  const effective = (key: string) => overrides[key] || scalar(profileValues[key]);
  const groups = visibleGroups(catalog, { mode, level, query, overrides });
  const searchId = `${idPrefix}settings-search`;

  return (
    <div className="settings-form">
      <input
        id={searchId}
        type="search"
        className="settings-search"
        data-testid={searchId}
        placeholder="Search settings"
        aria-label="Search settings"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {groups.length === 0 && (
        <p className="hint" data-testid={`${idPrefix}settings-empty`}>
          No setting matches in this mode.
        </p>
      )}
      {groups.map((group) => {
        const edited = group.settings.filter((setting) => overrides[setting.key]?.trim()).length;
        return (
          <details
            key={group.id}
            className="settings-group"
            data-testid={`${idPrefix}settings-group-${group.id}`}
            open={query.trim() !== "" || opened.has(group.id)}
            onToggle={(event) => {
              if (query.trim() !== "") return;
              const open = event.currentTarget.open;
              setOpened((current) => {
                const next = new Set(current);
                if (open) next.add(group.id);
                else next.delete(group.id);
                return next;
              });
            }}
          >
            <summary>
              {group.label}
              <span className="count">{edited > 0 ? `${edited} changed · ` : ""}{group.settings.length}</span>
            </summary>
            {group.settings.map((setting) => (
              <SettingControl
                key={setting.key}
                idPrefix={idPrefix}
                setting={setting}
                profileValue={scalar(profileValues[setting.key]) ?? setting.default}
                value={overrides[setting.key] ?? ""}
                invalid={setting.key === invalidKey}
                disabled={Boolean(setting.enabled_by) && !isOn(effective(setting.enabled_by!))}
                onChange={(value) => onChange(setting.key, value)}
              />
            ))}
          </details>
        );
      })}
    </div>
  );
}

function SettingControl(props: {
  idPrefix: string;
  setting: SettingDefinition;
  profileValue: string | undefined;
  value: string;
  invalid: boolean;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { idPrefix, setting, profileValue, value, invalid, disabled, onChange } = props;
  const testId = `${idPrefix}setting-${setting.key}`;
  const id = `${idPrefix}setting-input-${setting.key}`;
  const shown = profileValue ?? "";

  let control;
  if (BOOLEAN_TYPES.has(setting.type)) {
    control = (
      <select id={id} data-testid={testId} value={value} disabled={disabled} aria-invalid={invalid} onChange={(e) => onChange(e.target.value)}>
        <option value="">Profile ({isOn(shown) ? "on" : "off"})</option>
        <option value="1">On</option>
        <option value="0">Off</option>
      </select>
    );
  } else if (ENUM_TYPES.has(setting.type) && setting.enum) {
    const profileLabel = setting.enum.find((item) => item.value === shown)?.label ?? shown;
    control = (
      <select id={id} data-testid={testId} value={value} disabled={disabled} aria-invalid={invalid} onChange={(e) => onChange(e.target.value)}>
        <option value="">Profile ({profileLabel})</option>
        {setting.enum.map((item) => (
          <option key={item.value} value={item.value}>
            {item.label}
          </option>
        ))}
      </select>
    );
  } else if (setting.multiline) {
    control = (
      <textarea
        id={id}
        rows={3}
        data-testid={testId}
        placeholder={shown}
        value={value}
        disabled={disabled}
        aria-invalid={invalid}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else {
    // Text rather than type="number": percentages and the engine's own
    // serialized forms must round-trip unchanged. The API checks the range.
    const numeric = NUMERIC_TYPES.has(setting.type);
    control = (
      <input
        id={id}
        type="text"
        inputMode={numeric ? "decimal" : undefined}
        data-testid={testId}
        placeholder={shown}
        value={value}
        disabled={disabled}
        aria-invalid={invalid}
        aria-describedby={setting.unit ? `${id}-unit` : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <div className="setting" title={setting.tooltip}>
      <label htmlFor={id}>{setting.label}</label>
      {control}
      <span className="unit" id={`${id}-unit`}>
        {setting.unit}
      </span>
    </div>
  );
}
