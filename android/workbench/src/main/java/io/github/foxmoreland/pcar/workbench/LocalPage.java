package io.github.foxmoreland.pcar.workbench;

import android.content.Context;
import android.net.Uri;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.webkit.WebViewAssetLoader;
import java.io.ByteArrayInputStream;
import java.util.Map;
import java.util.Set;

final class LocalPage extends WebViewClient {
    static final String ORIGIN = "https://appassets.androidplatform.net";
    static final String PAGE = ORIGIN + "/workbench/index.html";
    private static final Set<String> ASSETS = Set.of("index.html", "appearance.js", "base.js", "persona_import.js",
            "android-transport.js", "android-ui.js", "android.css");
    private final WebViewAssetLoader loader;

    LocalPage(Context context) {
        WebViewAssetLoader.AssetsPathHandler assets = new WebViewAssetLoader.AssetsPathHandler(context);
        loader = new WebViewAssetLoader.Builder()
                .addPathHandler("/workbench/", path -> ASSETS.contains(path) ? assets.handle("workbench/" + path) : null)
                .build();
    }

    @Override
    public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
        return !request.isForMainFrame() || !isPage(request.getUrl().toString());
    }

    @Override
    public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
        Uri url = request.getUrl();
        if (isOrigin(url) && "GET".equals(request.getMethod()) && url.getQuery() == null) {
            WebResourceResponse resource = loader.shouldInterceptRequest(url);
            if (resource != null) return resource;
        }
        return new WebResourceResponse("text/plain", "utf-8", 403, "Blocked",
                Map.of("Cache-Control", "no-store"), new ByteArrayInputStream(new byte[0]));
    }

    static boolean isOrigin(Uri url) {
        return "https".equals(url.getScheme()) && "appassets.androidplatform.net".equals(url.getHost())
                && (url.getPort() == -1 || url.getPort() == 443) && url.getUserInfo() == null;
    }

    static boolean isPage(String url) {
        if (url == null) return false;
        Uri uri = Uri.parse(url);
        return isOrigin(uri) && "/workbench/index.html".equals(uri.getEncodedPath()) && uri.getQuery() == null;
    }
}
