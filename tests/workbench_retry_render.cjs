// Exercise the production renderer with an in-memory desktop bridge.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../persona_runtime/workbench_web/persona_import.js"), "utf8");

function renderer(name) {
  const start = source.indexOf("  function " + name + "(");
  const end = source.indexOf("\n  }", start);
  assert(start >= 0 && end > start);
  return source.slice(start, end + 4);
}

const session = {id: "session", version: 1};
const calls = [];
const notifications = [];
const details = [];
const panels = [];
const node = () => ({style: {}, children: []});
const mock = {
  state: {community: false}, linkState: "online", lastHealth: null,
  quotaSettings: () => {}, backupPanel: () => {},
  document: {createElement: node}, dialog: {contains: () => false}, run: fn => fn,
  currentPerson: () => ({id: "persona"}), currentSession: () => session,
  newId: () => "operation", arm: () => {}, notify: message => notifications.push(message),
  api: () => ({
    live_stage_retry: async (...args) => {
      calls.push(args);
      return {state: "awaiting_receipt"};
    },
    live_reply_detail: async (...args) => {
      details.push(args);
      return {body: "801", notes: "802", sources: []};
    },
  }),
  panel: title => {
    const box = {...node(), text: title};
    panels.push(box);
    return box;
  },
  groupBox: (parent, title) => mock.el("details", title, parent),
  makeBubble: (parent, role, text) => mock.el("p", text, parent),
  stamp: item => item,
  el: (tag, text, parent) => {
    const child = {...node(), tag, text};
    parent.children.push(child);
    return child;
  },
};
vm.runInNewContext(["button", "turnNode", "turnDetailButton", "turnResearchActions", "diagnostics"].map(renderer).join("\n"), mock);

async function check() {
  for (const status of ["completed", "queued", "running", "cancelled"]) {
    assert.equal(mock.turnNode({status}).children.filter(x => x.text === "重试").length, 0);
  }
  for (const reason of ["upstream_no_reply", "upstream_error"]) {
    const card = mock.turnNode({id: "turn", status: "failed", error_code: reason,
                                stage_retry: {reason, version: "stage"}});
    const buttons = card.children.filter(x => x.text === "重试");
    assert.equal(buttons.length, 1);
    await buttons[0].onclick();
  }
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], ["persona", "session", "turn", "stage", "operation"]);
  assert.equal(notifications.length, 2);

  const turn = {id: "turn", status: "completed", lookup_confirmation: "scope",
                feedback_available: true, research_detail: true, has_retrieved_notes: true,
                retained_lookup: true, role_long_detail: true, input_members: [{text: "701"}]};
  const labels = row => row.children.map(item => item.text);
  const technical = ["这条不像", "查看本轮查找资料", "查看本轮检索资料", "本轮沿用了上轮查回的资料。"];
  assert(technical.every(text => labels(mock.turnNode(turn)).includes(text)));

  mock.state.community = true;
  const community = mock.turnNode(turn);
  assert(technical.every(text => !labels(community).includes(text)));
  assert(labels(community).includes("701"));
  assert(labels(community).includes("确认本轮查找范围"));
  await community.children.find(item => item.text === "查看角色长文").onclick();
  assert.deepEqual(details.at(-1), ["persona", "session", "turn", "long"]);
  const progress = labels(mock.turnNode({status: "running", phase: "research_assessing"}));
  assert.deepEqual(progress, ["停止本轮", "正在回复"]);
  const failed = labels(mock.turnNode({status: "failed", error_code: "knowledge_study_failed",
                                      stage_retry: {version: "stage"}}));
  assert(failed.includes("重试"));
  assert(failed.includes("资料已查回，整理请求失败。"));

  session.turns = [turn];
  mock.diagnostics();
  const section = panels.at(-1).children.find(item => item.text === "会话资料与反馈");
  const round = section.children.find(item => item.text === "701");
  assert(technical.every(text => labels(round).includes(text)));
  await round.children.find(item => item.text === "查看本轮查找资料").onclick();
  assert.deepEqual(details.at(-1), ["persona", "session", "turn", "research"]);
  assert.equal(panels.at(-1).children[0].text, "802");
}
check().catch(error => {console.error(error); process.exitCode = 1;});
