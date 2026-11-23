package io.github.foxmoreland.pcar.runtimecheck;

final class NativeTokenizers {
    static {
        System.loadLibrary("pcar_tokenizers");
    }

    static native void open(int which, String modelPath);
    static native int[] encodeBge(String text, boolean special);
    static native int countFlash(String text);
}
