import { describe, expect, it } from "vitest";
import { loadRelayConfig } from "../src/config.js";
import { env, route } from "./fixtures/extra-routes.js";

describe("A2 次级路由配置", () => {
  it.each([undefined, "", " \n ", "[]"])("未启用时保留主路由 %s", (value) => {
    const config = loadRelayConfig({ ...env, RELAY_EXTRA_ROUTES_JSON: value });
    expect(config.extraRoutes).toEqual([]);
    expect(config.multicaWorkspaceId).toBe("grm");
    expect(config.multicaAssigneeId).toBe("router-agent");
  });
  it("解析有序路由、默认 Agent 与主 API 凭据继承", () => {
    const config = loadRelayConfig(env);
    expect(config.extraRoutes[0]).toMatchObject({ id: "dorea", mode: "existing_thread_only",
      multicaAssigneeType: "agent", multicaAllowedAssigneeIds: ["reviewer"],
      multicaApiBaseUrl: env.MULTICA_API_BASE_URL, multicaApiToken: env.MULTICA_API_TOKEN });
  });
  it("支持独立 API token、scope 与 Team", () => {
    const config = loadRelayConfig({ ...env, LAB_TOKEN: "lab-test", RELAY_EXTRA_ROUTES_JSON: JSON.stringify([
      { ...route, multicaApiTokenEnv: "LAB_TOKEN", multicaApiBaseUrl: "https://lab.test/",
        multicaAssigneeType: "squad", multicaThreadScopeId: "stable" }, { ...route, id: "second" },
    ]) });
    expect(config.extraRoutes.map((item) => item.id)).toEqual(["dorea", "second"]);
    expect(config.extraRoutes[0]).toMatchObject({ multicaApiToken: "lab-test", multicaApiBaseUrl: "https://lab.test",
      multicaAssigneeType: "squad", multicaThreadScopeId: "stable" });
  });
  it.each(["{", "null", "{}", '[null]', '["text"]'])("拒绝非法 JSON / 结构 %s", (value) => {
    expect(() => loadRelayConfig({ ...env, RELAY_EXTRA_ROUTES_JSON: value })).toThrow("invalid_extra_routes:");
  });
  it.each([
    { id: "" }, { id: "A" }, { id: " dorea " }, { id: "a".repeat(65) }, { id: "a/b" }, { id: 1 },
    { mode: undefined }, { mode: "create" }, { allowedChannelIds: [] }, { allowedChannelIds: "all" },
    { allowedChannelIds: ["C-1"] }, { allowedChannelIds: [" C1"] }, { allowedChannelIds: [4] },
    { multicaWorkspaceId: " " }, { multicaProjectId: null }, { multicaAssigneeId: "" },
    { multicaAssigneeType: "human" }, { multicaAssigneeType: "" }, { multicaAssigneeType: null }, { allowedAssigneeIds: [] }, { allowedAssigneeIds: [""] },
    { allowedAssigneeIds: "reviewer" }, { multicaThreadScopeId: " " },
    { multicaApiBaseUrl: "http://lab.test" }, { multicaApiBaseUrl: "https://user:password@lab.test" },
    { multicaApiBaseUrl: "bad" }, { multicaApiBaseUrl: "https://lab.test?token=secret" },
    { multicaApiTokenEnv: "lower_case" }, { multicaApiTokenEnv: "TOKEN;cat" },
    { multicaApiTokenEnv: "MISSING_TOKEN" }, { multicaApiToken: "inline-secret" },
    { ignoreTextSubstrings: [""] }, { ignoreTextSubstrings: [1] }, { ignoreTextSubstrings: "marker" },
  ])("拒绝非法字段且不泄漏输入 %j", (patch) => {
    expect(() => loadRelayConfig({ ...env, RELAY_EXTRA_ROUTES_JSON: JSON.stringify([{ ...route, ...patch }]) }))
      .toThrow(/^invalid_extra_routes:/);
    try { loadRelayConfig({ ...env, RELAY_EXTRA_ROUTES_JSON: JSON.stringify([{ ...route, ...patch }]) }); }
    catch (error) { expect(String(error)).not.toContain("inline-secret"); }
  });
  it("拒绝重复 id 和空的引用 token，接受空忽略列表", () => {
    expect(() => loadRelayConfig({ ...env, RELAY_EXTRA_ROUTES_JSON: JSON.stringify([route, route]) }))
      .toThrow("[1].id");
    expect(() => loadRelayConfig({ ...env, LAB_TOKEN: " ", RELAY_EXTRA_ROUTES_JSON: JSON.stringify([
      { ...route, multicaApiTokenEnv: "LAB_TOKEN" },
    ]) })).toThrow("missing_env:LAB_TOKEN");
    expect(loadRelayConfig({ ...env, RELAY_EXTRA_ROUTES_JSON: JSON.stringify([
      { ...route, ignoreTextSubstrings: [] },
    ]) }).extraRoutes[0]?.ignoreTextSubstrings).toEqual([]);
  });
});
