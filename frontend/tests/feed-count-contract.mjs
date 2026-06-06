import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const app = readFileSync(join(root, "src", "App.vue"), "utf8");
const styles = readFileSync(join(root, "src", "styles.css"), "utf8");

function assertIncludes(source, needle, message) {
  if (!source.includes(needle)) {
    throw new Error(message);
  }
}

assertIncludes(app, "feed-result-count", "Feed header should include a visible current result count.");
assertIncludes(app, "{{ items.length }}", "Feed result count should use the currently rendered item count.");
assertIncludes(styles, ".feed-result-count", "Feed result count should have dedicated styling.");
assertIncludes(styles, ".sort-select", "Feed sort control should have dedicated compact styling.");
assertIncludes(styles, "width: auto;", "Feed sort control should size to its content instead of using a wide input width.");
assertIncludes(styles, "min-height: 36px;", "Feed sort control should match the feed pill height.");
assertIncludes(styles, "border-radius: 999px;", "Feed sort control should use the same pill shape as adjacent controls.");

console.log("Feed result count contract passed.");
