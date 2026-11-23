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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class WorkbenchHost {
    interface Operation {
        String perform(PyObject module) throws Exception;
    }

    interface Outcome {
        void result(String value, String error);
    }

    private static WorkbenchHost instance;
    private final Context context;
    private final RuntimeBridge bridge;
    private final ExecutorService requests = Executors.newSingleThreadExecutor();
    private final ExecutorService lifecycle = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile PyObject module;
    private volatile boolean foreground;

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
        requests.execute(() -> {
            String value = null;
            String error = null;
            try {
                value = operation.perform(ensureStarted());
            } catch (Exception | LinkageError failure) {
                error = readableError(failure);
            }
            String result = value;
            String message = error;
            main.post(() -> callback.result(result, message));
        });
    }

    private PyObject ensureStarted() throws Exception {
        if (module == null) {
            clearFilePickerCache();
            if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
            PyObject candidate = Python.getInstance().getModule("pcar_workbench");
            String home = context.getNoBackupFilesDir().getCanonicalFile().toPath()
                    .resolve("community-runtime").toString();
            try {
                candidate.callAttr("start", bridge, home, foreground);
            } catch (RuntimeException failure) {
                Log.e("PCARWorkbench", "Workbench startup failed", failure);
                throw failure;
            }
            module = candidate;
            syncForeground();
        }
        return module;
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
