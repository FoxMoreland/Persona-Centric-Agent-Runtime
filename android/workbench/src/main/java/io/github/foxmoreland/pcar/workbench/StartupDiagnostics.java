package io.github.foxmoreland.pcar.workbench;

import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

public final class StartupDiagnostics {
    private static final int HISTORY_LIMIT = 32;
    private final ArrayDeque<JSONObject> events = new ArrayDeque<>();

    public void record(String phase, String error) {
        record(phase, error, null);
    }

    public synchronized void record(String phase, String error, String location) {
        if (events.size() == HISTORY_LIMIT) events.removeFirst();
        Map<String, Object> event = new LinkedHashMap<>();
        event.put("at", Instant.now().toString());
        event.put("phase", phase);
        if (error != null) event.put("error_type", error);
        if (location != null) event.put("location", location);
        events.addLast(new JSONObject(event));
    }

    synchronized String snapshot() {
        return new JSONArray(events).toString();
    }

    Path fallbackArchive(Context context, Path folder) throws IOException {
        String version = "unknown";
        try {
            version = context.getPackageManager().getPackageInfo(context.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException ignored) { }
        JSONObject manifest = new JSONObject(Map.of(
                "format", "pcar-support-logs-v1", "created_at", Instant.now().toString(),
                "edition", "community", "version", version, "os", "android",
                "os_build", Build.VERSION.SDK_INT, "scope", "startup"));
        Path staged = Files.createTempFile(folder, "pcar-boot-log-", ".zip");
        try {
            try (ZipOutputStream archive = new ZipOutputStream(Files.newOutputStream(staged))) {
                entry(archive, "manifest.json", manifest.toString());
                entry(archive, "android-startup.json", snapshot());
            }
            return staged;
        } catch (IOException | RuntimeException failure) {
            Files.deleteIfExists(staged);
            throw failure;
        }
    }

    private static void entry(ZipOutputStream archive, String name, String content) throws IOException {
        archive.putNextEntry(new ZipEntry(name));
        archive.write(content.getBytes(StandardCharsets.UTF_8));
        archive.closeEntry();
    }
}
