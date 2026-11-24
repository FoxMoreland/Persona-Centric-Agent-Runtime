package io.github.foxmoreland.pcar.workbench;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import io.github.foxmoreland.pcar.runtimecheck.RuntimeBridge;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class WorkbenchHost {
    interface Operation {
        String perform(PyObject module) throws Exception;
    }

    interface Outcome {
        void result(String value, String error);
    }

    interface ExportDestination {
        void write(Path staged) throws IOException;
    }

    private static WorkbenchHost instance;
    private final Context context;
    private final RuntimeBridge bridge;
    private final ExecutorService requests = Executors.newSingleThreadExecutor();
    private final ExecutorService lifecycle = Executors.newSingleThreadExecutor();
    private final ExecutorService exports = Executors.newSingleThreadExecutor();
    private final StartupDiagnostics diagnostics = new StartupDiagnostics();
    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile PyObject module;
    private volatile boolean foreground;
    private volatile boolean starting;

    static synchronized WorkbenchHost forApp(Context context) {
        if (instance == null) instance = new WorkbenchHost(context.getApplicationContext());
        return instance;
    }

    private WorkbenchHost(Context context) {
        this.context = context;
        bridge = new RuntimeBridge(context);
    }

    void foreground(boolean visible) {
        foreground = visible;
        if (module != null) lifecycle.execute(this::syncForeground);
    }

    void execute(Operation operation, Outcome callback) {
        executeOn(requests, () -> operation.perform(ensureStarted()), callback);
    }

    void exportSupportLogs(String args, ExportDestination destination, Outcome callback) {
        executeOn(exports, () -> {
            Path cache = context.getCacheDir().getCanonicalFile().toPath();
            PyObject current = module;
            Path staged = null;
            if (current != null && !starting) {
                try {
                    staged = Path.of(value(current, "make_support_export", home(), args,
                            cache.toString(), diagnostics.snapshot())).normalize();
                } catch (IOException | RuntimeException | LinkageError failure) {
                    diagnostics.record("diagnostic_collection", failure.getClass().getSimpleName());
                }
            }
            if (staged == null) staged = diagnostics.fallbackArchive(context, cache);
            destination.write(staged);
            return "true";
        }, callback);
    }

    private void executeOn(ExecutorService executor, Callable<String> operation, Outcome callback) {
        executor.execute(() -> {
            String value = null;
            String error = null;
            try {
                value = operation.call();
            } catch (Exception | LinkageError failure) {
                error = readableError(failure);
            }
            String result = value;
            String message = error;
            main.post(() -> callback.result(result, message));
        });
    }

    private String home() throws IOException {
        return context.getNoBackupFilesDir().getCanonicalFile().toPath().resolve("community-runtime").toString();
    }

    private PyObject ensureStarted() throws Exception {
        starting = true;
        String phase = "runtime_start";
        try {
            if (module == null) {
                clearFilePickerCache();
                phase = "python_start";
                diagnostics.record(phase, null);
                if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
                phase = "runtime_import";
                diagnostics.record(phase, null);
                module = Python.getInstance().getModule("pcar_workbench");
            }
            module.callAttr("start", bridge, home(), foreground, diagnostics);
            syncForeground();
            return module;
        } catch (Exception | LinkageError failure) {
            diagnostics.record(phase, failure.getClass().getSimpleName());
            Log.e("PCARWorkbench", "Workbench startup failed in " + phase, failure);
            throw new IOException("本机服务启动失败，正在自动重试。", failure);
        } finally {
            starting = false;
        }
    }

    private void clearFilePickerCache() {
        Path cache = context.getCacheDir().toPath();
        try (DirectoryStream<Path> entries = Files.newDirectoryStream(cache,
                "{pcar-import-*,pcar-export-*,PCAR-logs-*.zip,.pcar-log-*.tmp}")) {
            for (Path file : entries) {
                if (Files.isRegularFile(file)) Files.deleteIfExists(file);
            }
        } catch (IOException failure) {
            Log.w("PCARWorkbench", "Could not clear temporary picker files");
        }
    }

    private void syncForeground() {
        try {
            module.callAttr("set_foreground", foreground);
        } catch (RuntimeException failure) {
            Log.w("PCARWorkbench", "Could not update workbench foreground state");
        }
    }

    private static String readableError(Throwable error) {
        String message = error.getMessage();
        if (message == null || message.isBlank() || message.length() > 300 || message.contains("\n")) {
            return "安卓工作台当前无法完成此操作。";
        }
        if (message.contains("android_runtime_start_failed") || message.contains("android_runtime_disconnected")) {
            return "本机服务启动失败，请重新打开 PCAR。";
        }
        return message;
    }

    static String value(PyObject module, String method, Object... arguments) {
        return module.callAttr(method, arguments).toString();
    }
}
