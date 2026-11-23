package io.github.foxmoreland.pcar.runtimecheck;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class AndroidSecrets {
    private static final String STORE = "AndroidKeyStore";
    private static final String ALIAS = "pcar.local.v1";
    private static final String VERSION = "android-keystore-v1";
    private static final String CIPHER = "AES/GCM/NoPadding";

    synchronized String protect(String plain) throws Exception {
        Cipher cipher = Cipher.getInstance(CIPHER);
        cipher.init(Cipher.ENCRYPT_MODE, key(true));
        byte[] encrypted = cipher.doFinal(plain.getBytes(StandardCharsets.UTF_8));
        return VERSION + ":" + encode(cipher.getIV()) + ":" + encode(encrypted);
    }

    synchronized String unprotect(String token) throws Exception {
        String[] parts = token.split(":", -1);
        if (parts.length != 3 || !VERSION.equals(parts[0])) {
            throw new GeneralSecurityException("Invalid encrypted PCAR data");
        }
        byte[] nonce = Base64.decode(parts[1], Base64.NO_WRAP);
        byte[] payload = Base64.decode(parts[2], Base64.NO_WRAP);
        if (nonce.length != 12 || payload.length < 16) {
            throw new GeneralSecurityException("Invalid encrypted PCAR data");
        }
        Cipher cipher = Cipher.getInstance(CIPHER);
        cipher.init(Cipher.DECRYPT_MODE, key(false), new GCMParameterSpec(128, nonce));
        return new String(cipher.doFinal(payload), StandardCharsets.UTF_8);
    }

    private static String encode(byte[] bytes) {
        return Base64.encodeToString(bytes, Base64.NO_WRAP);
    }

    private static SecretKey key(boolean create) throws Exception {
        KeyStore store = KeyStore.getInstance(STORE);
        store.load(null);
        if (store.containsAlias(ALIAS)) {
            return (SecretKey) store.getKey(ALIAS, null);
        }
        if (!create) {
            throw new GeneralSecurityException("PCAR key is unavailable");
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, STORE);
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setKeySize(256)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build());
        return generator.generateKey();
    }
}
