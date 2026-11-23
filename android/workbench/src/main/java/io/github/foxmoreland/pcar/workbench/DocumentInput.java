package io.github.foxmoreland.pcar.workbench;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;

final class DocumentInput {
    enum Kind {
        TRANSCRIPT("transcript", ".md", 64L * 1024 * 1024, "请选择 Markdown 文件。", "聊天记录超过 64 MiB。"),
        SCOPED_TRANSCRIPT("scoped_transcript", ".md", 64L * 1024 * 1024, "请选择 Markdown 文件。", "聊天记录超过 64 MiB。"),
        CHARACTER("character", ".json", 256L * 1024, "请选择 PCAR 角色 JSON 文件。", "角色包超过 256 KiB。"),
        DEEPSEEK("deepseek", ".zip", 2L * 1024 * 1024 * 1024, "请选择 DeepSeek 导出 ZIP。", "导出 ZIP 超过 2 GiB。");

        final String operation;
        final String suffix;
        final long maximum;
        final String wrongType;
        final String tooLarge;

        Kind(String operation, String suffix, long maximum, String wrongType, String tooLarge) {
            this.operation = operation;
            this.suffix = suffix;
            this.maximum = maximum;
            this.wrongType = wrongType;
            this.tooLarge = tooLarge;
        }
    }

    static Path copy(Context context, Uri uri, Kind kind) throws IOException {
        if (!ContentResolver.SCHEME_CONTENT.equals(uri.getScheme())) throw new IOException(kind.wrongType);
        ContentResolver resolver = context.getContentResolver();
        try (Cursor cursor = resolver.query(uri, new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE},
                null, null, null)) {
            if (cursor == null || !cursor.moveToFirst()) throw new IOException("无法读取所选文件。");
            int nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
            String name = nameColumn < 0 ? null : cursor.getString(nameColumn);
            if (name == null || !name.toLowerCase(Locale.ROOT).endsWith(kind.suffix)) {
                throw new IOException(kind.wrongType);
            }
            int sizeColumn = cursor.getColumnIndex(OpenableColumns.SIZE);
            if (sizeColumn >= 0 && !cursor.isNull(sizeColumn) && cursor.getLong(sizeColumn) > kind.maximum) {
                throw new IOException(kind.tooLarge);
            }
        }

        Path temp = Files.createTempFile(context.getCacheDir().getCanonicalFile().toPath(), "pcar-import-", kind.suffix);
        boolean complete = false;
        try {
            try (InputStream input = resolver.openInputStream(uri); OutputStream output = Files.newOutputStream(temp)) {
                if (input == null) throw new IOException("无法读取所选文件。");
                byte[] buffer = new byte[64 * 1024];
                long total = 0;
                for (int count; (count = input.read(buffer)) != -1;) {
                    total += count;
                    if (total > kind.maximum) throw new IOException(kind.tooLarge);
                    output.write(buffer, 0, count);
                }
                if (total == 0) throw new IOException("所选文件为空。");
            }
            complete = true;
        } finally {
            if (!complete) Files.deleteIfExists(temp);
        }
        return temp;
    }
}
