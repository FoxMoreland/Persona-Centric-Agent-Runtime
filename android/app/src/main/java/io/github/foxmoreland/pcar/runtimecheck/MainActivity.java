package io.github.foxmoreland.pcar.runtimecheck;

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.nio.file.Path;
import java.util.concurrent.Executor;
import java.util.concurrent.Executors;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final String TAG = "PCARStage1";
    private static final Executor WORKER = Executors.newSingleThreadExecutor();
    private static RuntimeBridge bridge;
    private TextView status;
    private Button run;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (20 * getResources().getDisplayMetrics().density);
        content.setPadding(padding, padding, padding, padding);
        status = new TextView(this);
        status.setTextSize(18);
        status.setText("PCAR 安卓运行时验收");
        content.addView(status);
        run = new Button(this);
        run.setText("运行第一阶段检查");
        run.setOnClickListener(view -> startChecks());
        content.addView(run);
        setContentView(content);
        if (getIntent().getBooleanExtra("stage1", false)) {
            startChecks();
        }
    }

    private void startChecks() {
        run.setEnabled(false);
        status.setText("正在检查本机运行时…");
        WORKER.execute(() -> {
            String outcome;
            try {
                if (bridge == null) {
                    bridge = new RuntimeBridge(getApplicationContext());
                }
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                Path home = getNoBackupFilesDir().getCanonicalFile().toPath().resolve("stage1-runtime");
                String report = Python.getInstance().getModule("pcar_stage1")
                        .callAttr("run", bridge, home.toString()).toString();
                bridge.writeReport(report);
                outcome = new JSONObject(report).getString("status");
                Log.i(TAG, outcome);
            } catch (Exception | LinkageError failure) {
                Log.e(TAG, "Runtime check failed", failure);
                outcome = "failed";
                try {
                    bridge.writeReport(new JSONObject().put("status", outcome)
                            .put("stage", "android_host").put("error", failure.toString()).toString());
                } catch (Exception reporting) {
                    Log.e(TAG, "Cannot save check results", reporting);
                }
            }
            final boolean passed = outcome.equals("ok");
            runOnUiThread(() -> {
                if (!isDestroyed()) {
                    status.setText(passed ? "第一阶段检查通过" : "运行时检查遇到问题，请查看验收回执。");
                    run.setEnabled(true);
                }
            });
        });
    }

}
