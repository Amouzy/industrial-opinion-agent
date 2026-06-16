import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const appVue = readFileSync(join(root, "src", "App.vue"), "utf8");
const styles = readFileSync(join(root, "src", "styles.css"), "utf8");

function assertIncludes(source, needle, message) {
  if (!source.includes(needle)) {
    throw new Error(message);
  }
}

function assertOrder(source, first, second, message) {
  const firstIndex = source.indexOf(first);
  const secondIndex = source.indexOf(second);
  if (firstIndex < 0 || secondIndex < 0 || firstIndex >= secondIndex) {
    throw new Error(message);
  }
}

assertIncludes(appVue, "sourceIndustryHintOptions", "Source form should expose standardized industry hint options.");
assertIncludes(appVue, 'v-model="sourceForm.industry_hint"', "Source form should bind industry hint to a control.");
assertIncludes(appVue, 'v-for="[hint, label] in sourceIndustryHintOptions"', "Industry hint should render from standardized options.");
assertIncludes(appVue, "<select", "Source form should use select controls for standardized fields.");
assertIncludes(appVue, "source-form-priority", "Reliability score should be promoted into the first form row.");
assertOrder(appVue, "<span>类型</span>", "<span>权威分</span>", "Reliability score should appear immediately after type.");
assertOrder(appVue, "<span>行业提示</span>", "<span>检查间隔</span>", "Fetch interval should occupy the old reliability-score position.");
assertIncludes(appVue, "toggleSourceEnabled", "Source cards should toggle enabled state directly.");
assertIncludes(appVue, 'role="switch"', "Enabled control should be rendered as a switch.");
assertIncludes(appVue, "source-switch", "Enabled switch should have dedicated styling.");
assertIncludes(styles, ".source-switch", "Styles should include the enabled switch.");
assertIncludes(styles, ".source-switch-thumb", "Switch should include a visible thumb.");

console.log("Source configuration contract passed.");
