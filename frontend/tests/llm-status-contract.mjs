import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const appVue = readFileSync(join(root, "src", "App.vue"), "utf8");
const envExample = readFileSync(join(root, "..", "backend", ".env.example"), "utf8");

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

assertIncludes(appVue, "llmStatus", "UI should keep backend LLM runtime status in state.");
assertIncludes(appVue, 'request("/api/llm/status")', "UI should load the explicit backend LLM status endpoint.");
assertIncludes(appVue, "llmModeLabel", "UI should render a human-readable LLM mode label.");
assertIncludes(appVue, "llm-status-card", "UI should render a dedicated LLM status card.");
assertNotIncludes(appVue, "api_key", "UI must not reference or render API keys.");

assertIncludes(envExample, "LLM_PROVIDER=", "Backend env example should document LLM provider.");
assertIncludes(envExample, "LLM_MODEL=", "Backend env example should document LLM model.");
assertIncludes(envExample, "LLM_API_KEY=", "Backend env example should document LLM API key.");
assertIncludes(envExample, "LLM_BASE_URL=", "Backend env example should document OpenAI-compatible base URL.");

console.log("LLM status UI contract passed.");
