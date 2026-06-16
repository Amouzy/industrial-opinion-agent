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

assertIncludes(appVue, "runsPage", "Runs page should track the current page.");
assertIncludes(appVue, "runsPageSize", "Runs page should use an explicit page size.");
assertIncludes(appVue, "runsTotal", "Runs page should store the total run count returned by the API.");
assertIncludes(appVue, "/api/runs?page=", "Runs page should request paginated API data.");
assertIncludes(appVue, "loadRuns", "Runs page should centralize run list loading.");
assertIncludes(appVue, "runsTotalPages", "Runs page should compute total pages.");
assertIncludes(appVue, "goToRunsPage", "Runs page should expose a page navigation helper.");
assertIncludes(appVue, "run-pagination", "Runs list should render pagination controls.");
assertIncludes(appVue, "上一页", "Runs pagination should include a previous-page control.");
assertIncludes(appVue, "下一页", "Runs pagination should include a next-page control.");

assertIncludes(styles, ".run-pagination", "Runs pagination should have dedicated layout styling.");
assertIncludes(styles, ".run-page-button", "Runs pagination buttons should have dedicated styling.");
assertIncludes(styles, ".run-page-status", "Runs pagination status should have dedicated styling.");

console.log("Runs pagination contract passed.");
