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

function assertNotIncludes(source, needle, message) {
  if (source.includes(needle)) {
    throw new Error(message);
  }
}

assertIncludes(appVue, "expandedTraceNode", "Trace UI should keep one expanded workflow node.");
assertIncludes(appVue, 'expandedTraceNode = ref("")', "Trace UI should start with every workflow node collapsed.");
assertNotIncludes(appVue, 'expandedTraceNode = ref("classify_tag")', "Trace UI should not expand classify_tag by default.");
assertIncludes(appVue, "trace-chain", "Trace UI should render a vertical workflow chain.");
assertIncludes(appVue, "trace-step-button", "Trace nodes should be clickable workflow steps.");
assertIncludes(appVue, "traceNodeSummary", "Trace nodes should show a product-level summary before JSON details.");
assertNotIncludes(appVue, "item-trace-list", "Trace UI should not use the old stacked card list.");

assertIncludes(styles, ".trace-chain-item::before", "Trace UI should draw a vertical connector line.");
assertIncludes(styles, ".trace-step-marker", "Trace UI should style numbered workflow markers.");
assertIncludes(styles, ".trace-expand-enter-active", "Trace detail expansion should have a small transition.");

console.log("Trace UI contract passed.");
