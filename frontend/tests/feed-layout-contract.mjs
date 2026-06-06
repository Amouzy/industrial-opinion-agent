import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const styles = readFileSync(join(root, "src", "styles.css"), "utf8");

function assertIncludes(source, needle, message) {
  if (!source.includes(needle)) {
    throw new Error(message);
  }
}

assertIncludes(styles, "--feed-default-visible-items: 5;", "Feed layout should document the default five-item target.");
assertIncludes(styles, "--feed-default-height: 1120px;", "Feed list should be tall enough to show five intelligence cards by default.");
assertIncludes(styles, "--feed-expanded-height: 1200px;", "Expanded filter mode should keep at least five intelligence cards visible.");
assertIncludes(styles, "min-height: var(--feed-default-height);", "Feed list should use the default five-item height variable.");
assertIncludes(styles, ".workbench.expanded .feed-list", "Expanded workbench should keep a dedicated feed height rule.");
assertIncludes(styles, "min-height: var(--feed-expanded-height);", "Expanded feed list should use the expanded five-item height variable.");

console.log("Feed layout contract passed.");
