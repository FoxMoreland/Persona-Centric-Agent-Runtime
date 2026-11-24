(() => {
  "use strict";

  const channel = window.pcarAndroid;
  if (!channel) return;

  const pending = new Map();
  let sequence = 0;
  channel.onmessage = event => {
    const reply = JSON.parse(event.data);
    const request = pending.get(reply.id);
    if (!request) return;
    pending.delete(reply.id);
    if (reply.ok) request.resolve(reply.value);
    else request.reject(new Error(reply.error));
  };

  function call(method, args) {
    if (method === "presentation_visible" && document.documentElement.dataset.mobileTab !== "chat") {
      return Promise.resolve(false);
    }
    const id = String(++sequence);
    return new Promise((resolve, reject) => {
      pending.set(id, {resolve, reject});
      try {
        channel.postMessage(JSON.stringify({id, method, args}));
      } catch (error) {
        pending.delete(id);
        reject(error);
      }
    });
  }

  const names = [
    "release_announcements", "acknowledge_announcement",
    "appearance", "commit_import_scope", "exit_workbench", "export_support_logs", "import_action",
    "import_deepseek_session", "live_ack_submissions", "live_activate", "live_batch_action", "live_branch", "live_cancel",
    "live_confirm_inline_main", "live_create", "live_draft", "live_health", "live_location", "live_lookup_confirm",
    "live_memory", "live_message_position", "live_network_wake", "live_package", "live_page", "live_people", "live_persona_image",
    "live_package_sessions", "live_preview_package_export", "live_discard_package", "live_export_package",
    "live_jev",
    "live_privacy", "live_quota_decide", "live_quota_settings", "live_rename_session", "live_reply_detail", "live_save_package", "live_select",
    "live_set_buffer", "live_stage_retry", "live_submissions", "live_submit", "open_key_settings", "pick_deepseek_export", "pick_transcript",
    "presentation_attach", "presentation_begin", "presentation_decide", "presentation_reconcile",
    "presentation_visible", "recover_document", "revise_deepseek_markdown", "save_accent", "save_appearance",
    "save_deepseek_markdown", "template_spec"
  ];
  const methods = Object.fromEntries(names.map(name => [name, (...args) => call(name, args)]));
  window.pywebview = Object.freeze({api: Object.freeze(methods)});
})();
