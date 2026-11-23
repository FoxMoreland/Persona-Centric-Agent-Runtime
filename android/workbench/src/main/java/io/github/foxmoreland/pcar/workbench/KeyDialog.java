package io.github.foxmoreland.pcar.workbench;

import android.app.Activity;
import android.app.AlertDialog;
import android.text.InputFilter;
import android.text.InputType;
import android.text.method.PasswordTransformationMethod;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import org.json.JSONException;
import org.json.JSONObject;

final class KeyDialog {
    private final Activity activity;
    private final WorkbenchHost host;
    private final WorkbenchHost.Outcome onClose;
    private AlertDialog dialog;
    private AlertDialog confirmation;
    private String result = "false";
    private boolean open = true;

    KeyDialog(Activity activity, WorkbenchHost host, WorkbenchHost.Outcome onClose) {
        this.activity = activity;
        this.host = host;
        this.onClose = onClose;
    }

    void show() {
        host.execute(module -> Boolean.toString(module.callAttr("key_configured").toBoolean()),
                (value, error) -> {
                    if (!open || activity.isDestroyed()) return;
                    if (error != null) finish(error);
                    else showEditor("true".equals(value));
                });
    }

    private void showEditor(boolean saved) {
        LinearLayout layout = new LinearLayout(activity);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        int inset = (int) (24 * activity.getResources().getDisplayMetrics().density);
        layout.setPadding(inset, 0, inset, 0);
        TextView state = new TextView(activity);
        state.setText(saved ? "已保存 Key。填写新 Key 可替换。" : "填入 DeepSeek 官网申请的 API Key。");
        layout.addView(state);
        EditText key = new EditText(activity);
        key.setId(View.generateViewId());
        key.setSingleLine();
        key.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        key.setTransformationMethod(PasswordTransformationMethod.getInstance());
        key.setImeOptions(EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING);
        key.setHint("DeepSeek API Key");
        key.setSaveEnabled(false);
        key.setFilters(new InputFilter[]{new InputFilter.LengthFilter(512)});
        layout.addView(key);

        dialog = new AlertDialog.Builder(activity).setTitle("DeepSeek 官方连接").setView(layout)
                .setNegativeButton("取消", null).setPositiveButton("保存 Key", null).create();
        dialog.setOnDismissListener(ignored -> {
            key.getText().clear();
            finish(null);
        });
        dialog.setOnShowListener(ignored -> {
            secure(dialog);
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(button -> {
                String secret = key.getText().toString().trim();
                if (secret.isEmpty()) {
                    key.setError("请填写 Key。");
                    return;
                }
                button.setEnabled(false);
                host.execute(module -> WorkbenchHost.value(module, "change_key", secret),
                        (value, error) -> previewFinished(button, key, secret, value, error));
            });
        });
        dialog.show();
    }

    private void previewFinished(View save, EditText key, String secret, String value, String error) {
        if (!open || !dialog.isShowing()) return;
        if (error != null) {
            save.setEnabled(true);
            key.setError("暂时无法保存，请检查 Key 后重试。");
            return;
        }
        try {
            JSONObject preview = new JSONObject(value);
            String signature = preview.getString("confirmation");
            int affected = preview.getInt("affected");
            if (affected == 0) {
                commit(save, key, secret, signature);
            } else {
                confirmation = new AlertDialog.Builder(activity).setTitle("更新会话授权")
                        .setMessage(affected + " 个已授权会话将使用新的 DeepSeek Key。")
                        .setNegativeButton("取消", (popup, which) -> save.setEnabled(true))
                        .setPositiveButton("保存并更新授权", (popup, which) -> commit(save, key, secret, signature))
                        .create();
                confirmation.setOnCancelListener(popup -> save.setEnabled(true));
                confirmation.setOnShowListener(popup -> secure(confirmation));
                confirmation.show();
            }
        } catch (JSONException invalid) {
            save.setEnabled(true);
            key.setError("暂时无法核对会话授权。");
        }
    }

    private void commit(View save, EditText key, String secret, String signature) {
        View cancel = dialog.getButton(AlertDialog.BUTTON_NEGATIVE);
        cancel.setEnabled(false);
        dialog.setCancelable(false);
        host.execute(module -> WorkbenchHost.value(module, "change_key", secret, signature),
                (value, error) -> {
                    if (!open || !dialog.isShowing()) return;
                    if (error == null) {
                        result = "true";
                        dialog.dismiss();
                    } else {
                        dialog.setCancelable(true);
                        cancel.setEnabled(true);
                        save.setEnabled(true);
                        key.setError("保存未完成，请重新核对后再试。");
                    }
                });
    }

    private static void secure(AlertDialog target) {
        if (target.getWindow() != null) target.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
    }

    void close() {
        if (confirmation != null) confirmation.dismiss();
        if (dialog != null) dialog.dismiss();
        finish(null);
    }

    private void finish(String error) {
        if (!open) return;
        open = false;
        onClose.result(result, error);
    }
}
