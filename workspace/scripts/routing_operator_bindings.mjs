import { readFileSync } from "node:fs";
import path from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

const workspace = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
function readValues() {
  const selected = process.env.OPENCLAW_OPERATOR_CONFIG ?? path.join(workspace, "operator.json");
  if (!path.isAbsolute(selected) || /[~$]/.test(selected)) throw new Error("operator_configuration_path_not_absolute");
  let values;
  try { values = JSON.parse(readFileSync(selected, "utf8")); }
  catch (error) {
    if (error.code === "ENOENT" && !process.env.OPENCLAW_OPERATOR_CONFIG) return {};
    throw new Error("operator_configuration_unreadable");
  }
  if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error("operator_configuration_not_object");
  const version = Object.hasOwn(values, "schema_version") ? values.schema_version : 1;
  if (!Number.isInteger(version) || version !== 1) throw new Error("unsupported_operator_schema_version");
  return values;
}
const values = readValues();
function boundValue(key) {
  let value = values;
  for (const part of key.split(".")) {
    if (!value || typeof value !== "object" || Array.isArray(value) || !Object.hasOwn(value, part)) {
      if (key === "paths.workspace") return workspace;
      if (key === "paths.host_home") return homedir();
      return undefined;
    }
    value = value[part];
  }
  return value;
}
const placeholder = /<[^>]+>|\$\{|\{\{|\b(?:REPLACE_ME|CHANGEME)\b/i;
export function binding(key) {
  const value = boundValue(key);
  if (typeof value !== "string" || !value.trim() || (placeholder.test(value) || /[\u0000-\u001f\u007f]/.test(value))) return `\${operator:${key}}`;
  if (key.startsWith("paths.") && (!path.isAbsolute(value) || value.split(path.sep).includes("..") || /[~$]/.test(value))) throw new Error("operator_path_invalid");
  return value;
}
export function requireResolved(value) {
  if (JSON.stringify(value).includes("${operator:")) throw new Error("selected_route_operator_binding_missing");
}
export function materializeOperatorValue(value) {
  if (Array.isArray(value)) return value.map(materializeOperatorValue);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, materializeOperatorValue(item)]));
  if (typeof value === "string") {
    const match = /^\$\{operator:([A-Za-z0-9_.-]+)\}$/.exec(value);
    if (match) {
      if (match[1].startsWith("paths.")) return binding(match[1]);
      const bound = boundValue(match[1]);
      return bound == null || placeholder.test(JSON.stringify(bound)) ? value : structuredClone(bound);
    }
    if (value.includes("${operator:")) throw new Error("operator_reference_must_occupy_whole_value");
  }
  return value;
}
