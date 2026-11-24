package io.github.foxmoreland.pcar.workbench;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.res.Configuration;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowInsets;
import android.webkit.WebSettings;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;

public final class MainActivity extends Activity {
    private static final int PICK_DOCUMENT = 131;
    private static final int SAVE_DOCUMENT = 132;
    private static final String CHANNEL = "pcarAndroid";
    private FrameLayout root;
    private WebView page;
    private WorkbenchHost host;
    private boolean resumed;
    private PendingDocument pendingDocument;
    private PendingExport pendingExport;
    private JSONObject recoveredDocument;
    private KeyDialog keyDialog;

    record PendingDocument(DocumentInput.Kind kind, Reply reply) {}
    record PendingExport(String method, String args, Reply reply) {}

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        host = WorkbenchHost.forApp(this);
        if (state != null) {
            String input = state.getString("pending_document");
            String export = state.getString("pending_export");
            if (input != null) pendingDocument = new PendingDocument(DocumentInput.Kind.valueOf(input), null);
            if (export != null) pendingExport = new PendingExport(export, state.getString("pending_export_args"), null);
        }
        getWindow().setDecorFitsSystemWindows(false);
        root = new FrameLayout(this);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            Insets keyboard = insets.getInsets(WindowInsets.Type.ime());
            view.setPadding(bars.left, bars.top, bars.right, Math.max(bars.bottom, keyboard.bottom));
            return WindowInsets.CONSUMED;
        });
        boolean night = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        root.setBackgroundColor(night ? 0xff202923 : 0xffe9eee6);
        int lightBars = android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                | android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
        setContentView(root);
        root.post(() -> {
            android.view.WindowInsetsController controller = root.getWindowInsetsController();
            if (controller != null) controller.setSystemBarsAppearance(night ? 0 : lightBars, lightBars);
        });
        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::back);
        }
        openPage();
    }

    private void openPage() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            TextView error = new TextView(this);
            error.setText("请更新 Android System WebView 后重新打开 PCAR。");
            root.addView(error);
            return;
        }
        page = new WebView(this);
        WebView.setWebContentsDebuggingEnabled((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        WebSettings settings = page.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        page.setWebChromeClient(new WebChromeClient());
        page.setWebViewClient(new LocalPage(this));
        WebViewCompat.addWebMessageListener(page, CHANNEL, Set.of(LocalPage.ORIGIN), this::receive);
        root.addView(page, new FrameLayout.LayoutParams(-1, -1));
        page.loadUrl(LocalPage.PAGE);
    }

    private void receive(WebView sender, WebMessageCompat message, Uri origin, boolean mainFrame,
                         JavaScriptReplyProxy target) {
        if (sender != page || !mainFrame || !LocalPage.isOrigin(origin) || !LocalPage.isPage(sender.getUrl())
                || message.getType() != WebMessageCompat.TYPE_STRING) return;
        String data = message.getData();
        if (data == null || data.length() > 512 * 1024) return;

        JSONObject envelope;
        try {
            envelope = new JSONObject(data);
        } catch (JSONException invalid) {
            return;
        }
        String id = envelope.optString("id");
        if (!id.matches("[0-9]{1,12}")) return;
        Reply reply = new Reply(sender, target, id);
        JSONArray args = envelope.optJSONArray("args");
        String method = envelope.optString("method");
        if (args == null || method.isEmpty()) {
            reply.error("安卓工作台请求格式无效。");
            return;
        }
        if (method.equals("presentation_visible")) {
            reply.complete(Boolean.toString(presentationVisible(args)), null);
            return;
        }
        if (!resumed && !method.equals("live_location") && !method.equals("live_draft")
                && !method.equals("presentation_decide")) {
            reply.error("android_workbench_background");
            return;
        }
        switch (method) {
            case "recover_document" -> {
                JSONObject value = recoveredDocument;
                recoveredDocument = null;
                reply.complete(value == null ? "null" : value.toString(), null);
            }
            case "pick_transcript" -> {
                if (args.length() == 0) pick(DocumentInput.Kind.TRANSCRIPT, reply);
                else if (args.length() == 1 && Boolean.TRUE.equals(args.opt(0))) {
                    pick(DocumentInput.Kind.SCOPED_TRANSCRIPT, reply);
                } else reply.error("安卓工作台操作参数无效。");
            }
            case "export_support_logs", "save_deepseek_markdown", "live_export_package" -> saveDocument(method, args, reply);
            case "pick_deepseek_export", "live_package", "open_key_settings", "exit_workbench" -> {
                if (args.length() != 0) {
                    reply.error("安卓工作台操作参数无效。");
                    return;
                }
                if (method.equals("pick_deepseek_export")) pick(DocumentInput.Kind.DEEPSEEK, reply);
                else if (method.equals("live_package")) pick(DocumentInput.Kind.CHARACTER, reply);
                else if (method.equals("open_key_settings")) keySettings(reply);
                else {
                    reply.complete("true", null);
                    moveTaskToBack(true);
                }
            }
            default -> host.execute(module -> WorkbenchHost.value(module, "dispatch", method, args.toString()),
                    reply::complete);
        }
    }

    private boolean presentationVisible(JSONArray point) {
        if (!resumed || !hasWindowFocus() || page == null || !page.isShown()) return false;
        if (point.length() == 0) return true;
        double x = point.optDouble(0);
        double y = point.optDouble(1);
        return point.length() == 2 && x >= 0 && y >= 0 && x < page.getWidth() && y < page.getHeight();
    }

    private void keySettings(Reply reply) {
        if (keyDialog != null) {
            reply.error("模型设置已打开。");
            return;
        }
        keyDialog = new KeyDialog(this, host, (value, error) -> {
            keyDialog = null;
            reply.complete(value, error);
        });
        keyDialog.show();
    }

    private void pick(DocumentInput.Kind kind, Reply reply) {
        if (pendingDocument != null || pendingExport != null) {
            reply.error("请完成当前文件选择。");
            return;
        }
        pendingDocument = new PendingDocument(kind, reply);
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("*/*")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivityForResult(intent, PICK_DOCUMENT);
        } catch (android.content.ActivityNotFoundException unavailable) {
            pendingDocument = null;
            reply.error("系统文件选择器不可用。");
        }
    }

    private void saveDocument(String method, JSONArray args, Reply reply) {
        if (pendingDocument != null || pendingExport != null) {
            reply.error("请完成当前文件选择。");
            return;
        }
        boolean logs = method.equals("export_support_logs");
        boolean role = method.equals("live_export_package");
        boolean portable = role && args.length() == 2 && args.opt(1) instanceof JSONObject;
        boolean valid = logs ? args.length() == 2
                : args.opt(0) instanceof String && (args.length() == 1 || portable);
        if (!valid) {
            reply.error("安卓工作台操作参数无效。");
            return;
        }
        String mime = portable ? "application/octet-stream"
                : role ? "application/json" : logs ? "application/zip" : "text/markdown";
        String title = portable ? "PCAR.pcarpack" : role ? "PCAR.json" : logs ? "PCAR-logs.zip" : "DeepSeek.md";
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType(mime)
                .putExtra(Intent.EXTRA_TITLE, title)
                .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        pendingExport = new PendingExport(method, args.toString(), reply);
        try {
            startActivityForResult(intent, SAVE_DOCUMENT);
        } catch (android.content.ActivityNotFoundException unavailable) {
            pendingExport = null;
            reply.error("系统文件选择器不可用。");
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state);
        if (pendingDocument != null) state.putString("pending_document", pendingDocument.kind().name());
        if (pendingExport != null) {
            state.putString("pending_export", pendingExport.method());
            state.putString("pending_export_args", pendingExport.args());
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent intent) {
        super.onActivityResult(requestCode, resultCode, intent);
        Uri uri = resultCode == RESULT_OK && intent != null ? intent.getData() : null;
        if (requestCode == SAVE_DOCUMENT) {
            finishExport(uri);
            return;
        }
        if (requestCode != PICK_DOCUMENT || pendingDocument == null) return;
        PendingDocument request = pendingDocument;
        pendingDocument = null;
        if (uri == null) {
            if (request.reply() != null) request.reply().complete("null", null);
            return;
        }
        host.execute(module -> {
            Path staged = DocumentInput.copy(getApplicationContext(), uri, request.kind());
            boolean retain = false;
            try {
                String value = WorkbenchHost.value(module, "accept_file", request.kind().operation, staged.toString());
                retain = request.kind() == DocumentInput.Kind.DEEPSEEK;
                return value;
            } finally {
                if (!retain) Files.deleteIfExists(staged);
            }
        }, (value, error) -> completeDocument(request, value, error));
    }

    private void completeDocument(PendingDocument request, String value, String error) {
        if (request.reply() != null) {
            request.reply().complete(value, error);
            return;
        }
        if (page == null || isDestroyed()) return;
        try {
            recoveredDocument = new JSONObject().put("kind", request.kind().operation);
            if (error == null) recoveredDocument.put("value", new JSONTokener(value).nextValue());
            else recoveredDocument.put("error", error);
            page.evaluateJavascript("window.pcarMobile?.recoverDocument()", null);
        } catch (JSONException invalid) {
            recoveredDocument = null;
            Toast.makeText(this, "无法读取所选文件。", Toast.LENGTH_SHORT).show();
        }
    }

    private void finishExport(Uri uri) {
        if (pendingExport == null) return;
        PendingExport request = pendingExport;
        pendingExport = null;
        if (uri == null) {
            if (request.reply() != null) request.reply().complete("null", null);
            return;
        }
        if (!ContentResolver.SCHEME_CONTENT.equals(uri.getScheme())) {
            completeExport(request, null, "请选择系统支持的保存位置。");
            return;
        }
        if (request.method().equals("export_support_logs")) {
            host.exportSupportLogs(request.args(), staged -> writeExport(uri, staged),
                    (value, error) -> completeExport(request, value, error));
            return;
        }
        host.execute(module -> {
            Path cache = getCacheDir().getCanonicalFile().toPath();
            Path staged = Path.of(WorkbenchHost.value(module, "make_export", request.method(),
                    request.args(), cache.toString())).normalize();
            writeExport(uri, staged);
            return "true";
        }, (value, error) -> completeExport(request, value, error));
    }

    private void writeExport(Uri uri, Path staged) throws IOException {
        Path cache = getCacheDir().getCanonicalFile().toPath();
        if (!cache.equals(staged.getParent())) throw new IOException("本机导出路径无效。");
        try (OutputStream output = getContentResolver().openOutputStream(uri, "wt")) {
            if (output == null) throw new IOException("无法写入所选位置。");
            Files.copy(staged, output);
        } finally {
            Files.deleteIfExists(staged);
        }
    }

    private void completeExport(PendingExport request, String value, String error) {
        if (request.reply() != null) request.reply().complete(value, error);
        else if (!isDestroyed()) {
            String message = error == null ? "文件已保存。" : "保存失败。请重新选择保存位置。";
            Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        host.foreground(true);
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        if (page != null) {
            page.onResume();
            page.evaluateJavascript("window.pcarMobile?.resume()", null);
        }
    }

    @Override
    protected void onPause() {
        resumed = false;
        if (page != null) page.onPause();
        super.onPause();
    }

    @Override
    protected void onStop() {
        if (page != null) page.evaluateJavascript("window.pcarMobile?.pause()", null);
        host.foreground(false);
        super.onStop();
    }

    private void back() {
        if (page == null) {
            moveTaskToBack(true);
            return;
        }
        page.evaluateJavascript("window.pcarMobile?.back() === true", handled -> {
            if (!"true".equals(handled) && !isDestroyed()) moveTaskToBack(true);
        });
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        back();
    }

    @Override
    protected void onDestroy() {
        if (keyDialog != null) keyDialog.close();
        pendingDocument = null;
        pendingExport = null;
        if (page != null) {
            WebViewCompat.removeWebMessageListener(page, CHANNEL);
            root.removeView(page);
            page.destroy();
            page = null;
        }
        super.onDestroy();
    }

    final class Reply {
        private final WebView sender;
        private final JavaScriptReplyProxy target;
        private final String requestId;

        Reply(WebView sender, JavaScriptReplyProxy target, String requestId) {
            this.sender = sender;
            this.target = target;
            this.requestId = requestId;
        }

        void complete(String value, String error) {
            if (isDestroyed() || sender != page || !LocalPage.isPage(sender.getUrl())) return;
            JSONObject reply = new JSONObject();
            try {
                reply.put("id", requestId);
                reply.put("ok", error == null);
                if (error != null) reply.put("error", error);
                else reply.put("value", new JSONTokener(value).nextValue());
            } catch (JSONException invalid) {
                error("本机返回的数据格式无效。");
                return;
            }
            target.postMessage(reply.toString());
        }

        void error(String message) {
            complete(null, message);
        }
    }
}
