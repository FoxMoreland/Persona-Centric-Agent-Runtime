package io.github.foxmoreland.pcar.runtimecheck;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.ProxyInfo;
import android.net.Uri;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;

public final class RuntimeBridge {
    private final Context context;
    private final AndroidSecrets secrets = new AndroidSecrets();
    private BgeSession bge;
    private String bgePath;

    public RuntimeBridge(Context context) {
        this.context = context.getApplicationContext();
    }

    public void installAsset(String asset, String destination) throws Exception {
        Path path = Path.of(destination);
        Files.createDirectories(path.getParent());
        Path partial = Files.createTempFile(path.getParent(), ".asset-", ".partial");
        try (InputStream input = context.getAssets().open(asset)) {
            Files.copy(input, partial, StandardCopyOption.REPLACE_EXISTING);
            Files.move(partial, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } finally {
            Files.deleteIfExists(partial);
        }
    }

    public void writeReport(String report) throws Exception {
        Path target = context.getFilesDir().toPath().resolve("stage1-report.json");
        Path partial = target.resolveSibling("stage1-report.partial");
        Files.write(partial, report.getBytes(StandardCharsets.UTF_8));
        Files.move(partial, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    public synchronized void openBge(String modelPath, String tokenizerPath) throws Exception {
        NativeTokenizers.open(0, tokenizerPath);
        if (bge == null) {
            bge = new BgeSession(modelPath);
            bgePath = modelPath;
        } else if (!bgePath.equals(modelPath)) {
            throw new IllegalStateException("A different BGE model is already active");
        }
    }

    public int[] bgeTokens(String text, boolean special) {
        return NativeTokenizers.encodeBge(text, special);
    }

    public float[] embedBge(int[] ids) throws Exception {
        BgeSession session;
        synchronized (this) {
            session = bge;
        }
        if (session == null) {
            throw new IllegalStateException("BGE session is unavailable");
        }
        return session.encode(ids);
    }

    public void openFlash(String tokenizerPath) {
        NativeTokenizers.open(1, tokenizerPath);
    }

    public int countFlash(String text) {
        return NativeTokenizers.countFlash(text);
    }

    public String protectSecret(String plain) throws Exception {
        return secrets.protect(plain);
    }

    public String unprotectSecret(String ciphertext) throws Exception {
        return secrets.unprotect(ciphertext);
    }

    public String systemHttpProxy() {
        ConnectivityManager connectivity = context.getSystemService(ConnectivityManager.class);
        if (connectivity == null) throw new IllegalStateException("Android proxy status is unavailable");
        ProxyInfo proxy = connectivity.getDefaultProxy();
        if (proxy == null) return null;
        if (!Uri.EMPTY.equals(proxy.getPacFileUrl())) {
            throw new IllegalStateException("PAC is unsupported by PCAR's system HTTP proxy transport");
        }
        String host = proxy.getHost();
        if (host == null || host.isBlank() || proxy.getPort() < 1) {
            throw new IllegalStateException("Android HTTP proxy is incomplete");
        }
        if (host.contains(":") && !host.startsWith("[")) host = "[" + host + "]";
        return "http://" + host + ":" + proxy.getPort();
    }
}
