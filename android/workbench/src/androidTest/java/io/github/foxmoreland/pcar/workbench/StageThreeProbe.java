package io.github.foxmoreland.pcar.workbench;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.os.Build;
import android.os.Bundle;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import io.github.foxmoreland.pcar.runtimecheck.RuntimeBridge;
import org.json.JSONObject;

import java.io.DataInputStream;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Local emulator acceptance companion. The workbench APK does not contain this runner. */
public final class StageThreeProbe extends Instrumentation {
    private Bundle options;

    @Override
    public void onCreate(Bundle bundle) {
        super.onCreate(bundle);
        options = bundle;
        start();
    }

    @Override
    public void onStart() {
        Context app = getTargetContext();
        Bundle report = new Bundle();
        boolean passed = false;
        String mode = options.getString("mode", "offline");
        try {
            if (!"io.github.foxmoreland.pcar.community.debug".equals(app.getPackageName())
                    || !"ranchu".equals(Build.HARDWARE)
                    || !"x86_64".equals(Build.SUPPORTED_ABIS[0])) {
                throw new IllegalStateException("Expected the x86_64 community debug emulator");
            }
            if ("offline".equals(mode)) {
                String result = offline(app);
                report.putString("offline_result", result);
                passed = new JSONObject(result).getBoolean("passed");
            } else if ("credential".equals(mode)) {
                installCredential(app);
                report.putString("credential_sealed", "true");
                passed = true;
            }
        } catch (Exception | LinkageError failure) {
            // Credential-path failures carry only a class name.
            report.putString("stage_error", "offline".equals(mode) ? failure.toString()
                    : failure.getClass().getSimpleName());
        }
        report.putString("passed", Boolean.toString(passed));
        finish(passed ? Activity.RESULT_OK : Activity.RESULT_CANCELED, report);
    }

    private String offline(Context app) throws Exception {
        Path root = app.getCacheDir().getCanonicalFile().toPath().resolve("stage3-offline");
        Path suite = root.resolve("tests");
        Files.createDirectories(suite);
        for (String asset : getContext().getAssets().list("suite/tests")) {
            try (InputStream source = getContext().getAssets().open("suite/tests/" + asset)) {
                Files.copy(source, suite.resolve(asset), StandardCopyOption.REPLACE_EXISTING);
            }
        }
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(app));
        }
        Python python = Python.getInstance();
        python.getModule("persona_runtime.android_platform").callAttr("bind", new RuntimeBridge(app));
        String code;
        try (InputStream source = getContext().getAssets().open("pcar_stage3_checks.py")) {
            code = new String(source.readAllBytes(), StandardCharsets.UTF_8);
        }
        PyObject globals = python.getBuiltins().callAttr("dict");
        globals.callAttr("__setitem__", "test_root", root.toString());
        python.getBuiltins().callAttr("exec", code, globals);
        return globals.callAttr("__getitem__", "result").toString();
    }

    private void installCredential(Context app) throws Exception {
        String challenge = options.getString("challenge", "");
        if (!challenge.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("Invalid local challenge");
        String proxy = options.getString("expected_proxy", "");
        if (!proxy.isEmpty() && !proxy.equals(new RuntimeBridge(app).systemHttpProxy())) {
            throw new IllegalStateException("Unexpected Android system proxy");
        }
        try (ServerSocket server = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            server.setSoTimeout(60_000);
            Bundle status = new Bundle();
            status.putInt("relay_port", server.getLocalPort());
            sendStatus(1, status);
            try (Socket connection = server.accept()) {
                connection.setSoTimeout(7000);
                DataInputStream input = new DataInputStream(connection.getInputStream());
                byte[] proof = input.readNBytes(64);
                if (!MessageDigest.isEqual(proof, challenge.getBytes(StandardCharsets.US_ASCII))) {
                    throw new SecurityException("Invalid local relay challenge");
                }
                int length = input.readUnsignedShort();
                if (length == 0 || length > 512) throw new IllegalArgumentException("Invalid credential length");
                byte[] secret = new byte[length];
                String key;
                try {
                    input.readFully(secret);
                    key = new String(secret, StandardCharsets.UTF_8);
                } finally {
                    Arrays.fill(secret, (byte) 0);
                }
                boolean sealed = sealFirstKey(app, key);
                connection.getOutputStream().write(sealed ? 1 : 0);
                connection.getOutputStream().flush();
                if (!sealed) throw new IllegalStateException("Credential was not sealed");
            }
        }
    }

    private boolean sealFirstKey(Context app, String key) throws InterruptedException {
        WorkbenchHost host = WorkbenchHost.forApp(app);
        host.foreground(true);
        CountDownLatch completed = new CountDownLatch(1);
        boolean[] saved = {false};
        host.execute(module -> {
            if (module.callAttr("key_configured").toBoolean()) throw new IllegalStateException("Expected a fresh instance");
            JSONObject preview = new JSONObject(WorkbenchHost.value(module, "change_key", key));
            if (preview.getInt("affected") != 0) throw new IllegalStateException("Unexpected session authorization change");
            WorkbenchHost.value(module, "change_key", key, preview.getString("confirmation"));
            return "ok";
        }, (value, error) -> {
            saved[0] = error == null && "ok".equals(value);
            completed.countDown();
        });
        return completed.await(55, TimeUnit.SECONDS) && saved[0];
    }
}
