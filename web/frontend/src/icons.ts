/**
 * The desktop application's own icons, imported straight from
 * `resources/images` rather than copied, so the browser and the desktop app
 * can never drift apart. Vite bundles each one as a hashed asset URL.
 *
 * Every use is decorative (`alt=""`): each control that shows one also has
 * its own accessible name.
 */
import logo from "../../../resources/images/OrcaSlicer.svg?url";
import printer from "../../../resources/images/printer.svg?url";
import filament from "../../../resources/images/filament.svg?url";
import process from "../../../resources/images/process.svg?url";
import open from "../../../resources/images/toolbar_open.svg?url";
import arrange from "../../../resources/images/toolbar_arrange.svg?url";
import duplicate from "../../../resources/images/add_copies.svg?url";
import remove from "../../../resources/images/delete.svg?url";
import addFilament from "../../../resources/images/add_filament.svg?url";
import removeFilament from "../../../resources/images/delete_filament.svg?url";
import prepare from "../../../resources/images/tab_3d_active.svg?url";
import preview from "../../../resources/images/tab_preview_active.svg?url";
import quality from "../../../resources/images/param_layer_height.svg?url";
import infill from "../../../resources/images/param_infill.svg?url";
import support from "../../../resources/images/param_support.svg?url";
import adhesion from "../../../resources/images/param_adhension.svg?url";
import speed from "../../../resources/images/param_speed.svg?url";
import temperature from "../../../resources/images/param_temperature.svg?url";
import advanced from "../../../resources/images/param_advanced.svg?url";
import settings from "../../../resources/images/param_settings.svg?url";

export const icons = {
  logo,
  printer,
  filament,
  process,
  open,
  arrange,
  duplicate,
  remove,
  addFilament,
  removeFilament,
  prepare,
  preview,
};

/**
 * The icon the desktop's settings tabs show beside each group title, by the
 * group ids the engine's settings catalog declares. A group the engine adds
 * later still renders, with the generic settings icon.
 */
const GROUP_ICONS: Record<string, string> = {
  quality,
  infill,
  support,
  adhesion,
  speed,
  filament: temperature,
  advanced,
};

export function groupIcon(group: string): string {
  return GROUP_ICONS[group] ?? settings;
}
