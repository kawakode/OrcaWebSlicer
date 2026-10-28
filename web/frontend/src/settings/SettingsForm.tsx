import type { ProfileValue, SettingDefinition, SettingsCatalog } from "../types";

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
 * Renders every catalog setting as an override control. Nothing about a
 * setting's type, unit, range, choices, or dependency is stated here: it all
 * comes from the engine's catalog, and the profile values are only shown as
 * what a blank override keeps.
 */
export function SettingsForm(props: {
  catalog: SettingsCatalog;
  profileValues: Record<string, ProfileValue>;
  overrides: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  const { catalog, profileValues, overrides, onChange } = props;
  const effective = (key: string) => overrides[key] || scalar(profileValues[key]);
  const available = catalog.settings.filter((setting) => !setting.missing);

  return (
    <>
      {catalog.groups.map((group) => {
        const settings = available.filter((setting) => setting.group === group.id);
        if (settings.length === 0) return null;
        return (
          <fieldset key={group.id} data-testid={`settings-group-${group.id}`}>
            <legend>{group.label}</legend>
            {settings.map((setting) => (
              <SettingControl
                key={setting.key}
                setting={setting}
                profileValue={scalar(profileValues[setting.key]) ?? setting.default}
                value={overrides[setting.key] ?? ""}
                disabled={Boolean(setting.enabled_by) && !isOn(effective(setting.enabled_by!))}
                onChange={(value) => onChange(setting.key, value)}
              />
            ))}
          </fieldset>
        );
      })}
    </>
  );
}

function SettingControl(props: {
  setting: SettingDefinition;
  profileValue: string | undefined;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { setting, profileValue, value, disabled, onChange } = props;
  const testId = `setting-${setting.key}`;
  const id = `setting-input-${setting.key}`;
  const shown = profileValue ?? "";

  let control;
  if (BOOLEAN_TYPES.has(setting.type)) {
    control = (
      <select id={id} data-testid={testId} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        <option value="">Profile ({isOn(shown) ? "on" : "off"})</option>
        <option value="1">On</option>
        <option value="0">Off</option>
      </select>
    );
  } else if (ENUM_TYPES.has(setting.type) && setting.enum) {
    const profileLabel = setting.enum.find((item) => item.value === shown)?.label ?? shown;
    control = (
      <select id={id} data-testid={testId} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        <option value="">Profile ({profileLabel})</option>
        {setting.enum.map((item) => (
          <option key={item.value} value={item.value}>
            {item.label}
          </option>
        ))}
      </select>
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
