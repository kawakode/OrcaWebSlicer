import { useId, useMemo } from "react";

import type { SettingDefinition, SettingsCatalog } from "./types";

/**
 * Renders one control per setting the engine declared. Nothing here knows what
 * a setting means: the control shape, its range, its options, and its unit all
 * come from `PrintConfigDef` by way of `GET /api/v1/settings`.
 */
export function SettingsForm(props: {
  catalog: SettingsCatalog;
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  disabled?: boolean;
}) {
  const { catalog, values, onChange, disabled } = props;
  const grouped = useMemo(() => {
    const available = catalog.settings.filter((setting) => !setting.missing);
    return catalog.groups
      .map((group) => ({
        ...group,
        settings: available.filter((setting) => setting.group === group.id),
      }))
      .filter((group) => group.settings.length > 0);
  }, [catalog]);

  return (
    <div className="settings">
      {grouped.map((group) => (
        <fieldset key={group.id} data-testid={`setting-group-${group.id}`}>
          <legend>{group.label}</legend>
          {group.settings.map((setting) => (
            <SettingControl
              key={setting.key}
              setting={setting}
              value={values[setting.key] ?? ""}
              // A setting the engine gates on another one is disabled only once
              // the user has switched that other one off here. While it is
              // unset the selected profile decides, and this app cannot know
              // what the profile chose without slicing it.
              disabled={disabled || isTurnedOff(setting.enabled_by, values)}
              onChange={(next) => onChange(setting.key, next)}
            />
          ))}
        </fieldset>
      ))}
    </div>
  );
}

function isTurnedOff(controller: string | undefined, values: Record<string, string>): boolean {
  if (!controller) return false;
  const value = values[controller];
  return value !== undefined && FALSE_LITERALS.has(value.trim().toLowerCase());
}

const FALSE_LITERALS = new Set(["0", "false", "no"]);
// Percentage-capable types stay out: the engine serializes them with a "%"
// suffix, which a number input would refuse to hold.
const NUMERIC_TYPES = new Set(["float", "floats", "int", "ints"]);

/** The placeholder every unset control shows: the profile keeps deciding. */
const INHERITED = "profile default";

function SettingControl(props: {
  setting: SettingDefinition;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { setting, value, disabled, onChange } = props;
  const id = useId();
  const describedBy = setting.tooltip ? `${id}-tooltip` : undefined;
  const testId = `setting-${setting.key}`;

  return (
    <div className="setting">
      <label htmlFor={id}>
        {setting.label || setting.key}
        {setting.unit && <span className="unit"> ({setting.unit})</span>}
      </label>
      {renderControl()}
      {setting.tooltip && (
        <p className="tooltip" id={describedBy}>
          {setting.tooltip}
        </p>
      )}
    </div>
  );

  function renderControl() {
    const shared = {
      id,
      disabled,
      "data-testid": testId,
      "aria-describedby": describedBy,
    } as const;

    if (setting.type === "bool" || setting.type === "bools") {
      // Three states: on, off, and "leave it to the profile", so the checkbox
      // is only ever an explicit choice.
      return (
        <select
          {...shared}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">{INHERITED}</option>
          <option value="1">On</option>
          <option value="0">Off</option>
        </select>
      );
    }

    if (setting.enum && setting.enum.length > 0) {
      return (
        <select {...shared} value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">{INHERITED}</option>
          {setting.enum.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      );
    }

    // A percentage-capable setting stays a text field: the engine accepts both
    // "40" and "40%", and a number input would silently drop the suffix.
    const numeric = NUMERIC_TYPES.has(setting.type) && !setting.vector;
    return (
      <input
        {...shared}
        type={numeric ? "number" : "text"}
        inputMode={numeric ? "decimal" : undefined}
        placeholder={setting.default ? `${INHERITED} (${setting.default})` : INHERITED}
        min={numeric ? setting.min : undefined}
        max={numeric ? setting.max : undefined}
        step={numeric && (setting.type === "int" || setting.type === "ints") ? 1 : "any"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
}


