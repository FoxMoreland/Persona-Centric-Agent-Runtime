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
const node = () => ({style: {}, children: []});
const mock = {
  document: {createElement: node}, dialog: {contains: () => false}, run: fn => fn,
  currentPerson: () => ({id: "persona"}), currentSession: () => session,
  newId: () => "operation", arm: () => {}, notify: message => notifications.push(message),
  api: () => ({live_stage_retry: async (...args) => {
    calls.push(args);
    return {state: "awaiting_receipt"};
  }}),
  el: (tag, text, parent) => {
    const child = {...node(), tag, text};
    parent.children.push(child);
    return child;
  },
};
vm.runInNewContext(renderer("button") + "\n" + renderer("turnNode"), mock);

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
}
check().catch(error => {console.error(error); process.exitCode = 1;});
