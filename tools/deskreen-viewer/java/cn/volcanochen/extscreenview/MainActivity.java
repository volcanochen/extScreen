package cn.volcanochen.extscreenview;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * 基于浏览器内核（WebView）的投屏接收界面。
 *
 * 零操作设计：
 *  - 界面打开即常驻前台（常亮 + 沉浸式），URL 由 ViewerService 通过 mDNS 自动发现；
 *  - 预写 Deskreen 的 GA 授权标记，隐私弹窗不会出现；
 *  - 房间号轮换（mDNS TXT ver 变化）→ ViewerService 写入新 URL → 本页自动重载；
 *  - 主页面加载失败 6 秒后自动重试。
 */
public class MainActivity extends Activity {

    private WebView web;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private String applied = "";
    private boolean pageError = false;

    private final Runnable retryRun = new Runnable() {
        @Override public void run() {
            String u = ViewerBridge.url;
            if (u != null && pageError && applied.equals(u)) {
                pageError = false;
                web.loadUrl(u);
                reportDevice();   // 自动重连后重报屏幕信息（PC 可能是新会话）
            }
        }
    };

    private final ViewerBridge.OnUrl urlListener = new ViewerBridge.OnUrl() {
        @Override public void onUrl(String u) {
            ui.post(new Runnable() {
                @Override public void run() { apply(ViewerBridge.url); }
            });
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // 锁屏上也直接显示投屏画面（PC 端 adb 唤醒后无需解锁即可看）；
        // API 27+，Note8 是 28。触摸控制需解锁后才有意义。
        if (android.os.Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }

        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled(true);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // WebRTC 画面自动播放
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        web.setBackgroundColor(Color.BLACK);

        web.setWebViewClient(new WebViewClient() {
            private static final String TAG = "ExtScreenView";

            @Override
            public void onPageStarted(WebView v, String u, Bitmap favicon) {
                android.util.Log.d(TAG, "page start: " + u);
                // Deskreen 观众页的隐私授权标记（key 见 deskreen 源码 analytics.ts）
                v.evaluateJavascript(
                    "try{localStorage.setItem('deskreen_ga_consent','accepted')}catch(e){}",
                    null);
            }

            @Override
            public void onPageFinished(WebView v, String u) {
                android.util.Log.d(TAG, "page finish: " + u);
                if (!pageError) retryNoop();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String u) {
                return false;   // 全部留在 WebView 里
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest rq,
                                        WebResourceError err) {
                if (rq != null && rq.isForMainFrame()) {
                    android.util.Log.d(TAG, "main frame error: "
                            + (err != null ? err.getDescription() : "?")
                            + " code=" + (err != null ? err.getErrorCode() : 0)
                            + " url=" + (rq.getUrl() != null ? rq.getUrl() : "?"));
                    onPageError();
                }
            }

            @Override
            public void onReceivedError(WebView v, int code, String desc,
                                        String failingUrl) {
                android.util.Log.d(TAG, "error(legacy): " + desc
                        + " code=" + code + " url=" + failingUrl);
                onPageError();
            }
        });

        setContentView(web);
        immersive();
        ViewerBridge.activityVisible = true;
        ViewerBridge.listener = urlListener;
        apply(ViewerBridge.url);

        // 调试通道：adb shell am start -n ... --es debug_url http://...
        // mDNS 不便的环境（沙箱/防火墙）里单独验证 WebView 链路用
        String dbg = getIntent() == null ? null : getIntent().getStringExtra("debug_url");
        if (dbg != null && !dbg.isEmpty()) {
            ViewerBridge.setUrl(dbg);
        }
        apply(ViewerBridge.url);

        // 服务可能已被 BootReceiver 拉起，这里幂等再启动一次
        android.content.Intent i = new android.content.Intent(this, ViewerService.class);
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            startForegroundService(i);
        } else {
            startService(i);
        }
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        if (intent != null && intent.getStringExtra("debug_url") != null) {
            ViewerBridge.setUrl(intent.getStringExtra("debug_url"));
            applied = "";   // USB 推送强制重载：PC 端重启投屏后立即恢复画面
            apply(ViewerBridge.url);
        }
    }

    private void retryNoop() {
        // 房间号还没轮换前加载成功，重置错误状态由 onPageError 控制
    }

    private void onPageError() {
        pageError = true;
        ui.removeCallbacks(retryRun);
        ui.postDelayed(retryRun, 6000);
    }

    private void apply(String u) {
        if (u == null || web == null) return;
        if (u.equals(applied) && !pageError) {
            // URL 没变也要上报：PC 重启投屏后是新会话，需要手机屏幕信息
            // 来适配窗口（页面本身靠 img 的断流重连自愈）
            reportDevice();
            return;
        }
        applied = u;
        pageError = false;
        web.loadUrl(u);
        reportDevice();   // 新 PC 会话需要手机屏幕信息来适配窗口
    }

    /**
     * 把手机屏幕分辨率/朝向上报给 PC（POST /device）。
     * PC 收到后按屏幕比例调整被投窗口，使画面铺满手机、不留黑边；
     * 手机旋转（onConfigurationChanged）时会重新上报。
     */
    private void reportDevice() {
        final String u = ViewerBridge.url;
        if (u == null || !u.startsWith("http://")) return;
        final Activity act = this;
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    java.net.URL url = new java.net.URL(u);
                    int port = url.getPort() > 0 ? url.getPort() : 80;
                    String base = "http://" + url.getHost() + ":" + port;
                    android.view.Display d =
                            act.getWindowManager().getDefaultDisplay();
                    android.graphics.Point p = new android.graphics.Point();
                    d.getRealSize(p);
                    org.json.JSONObject o = new org.json.JSONObject();
                    o.put("w", p.x);
                    o.put("h", p.y);
                    o.put("rot", d.getRotation());
                    byte[] body = o.toString().getBytes("UTF-8");
                    java.net.HttpURLConnection c =
                            (java.net.HttpURLConnection) new java.net.URL(
                                    base + "/device").openConnection();
                    c.setRequestMethod("POST");
                    c.setDoOutput(true);
                    c.setConnectTimeout(3000);
                    c.setReadTimeout(3000);
                    java.io.OutputStream os = c.getOutputStream();
                    os.write(body);
                    os.flush();
                    os.close();
                    c.getResponseCode();   // 不读响应不算发送完成
                    c.disconnect();
                } catch (Exception ignored) {}
            }
        }).start();
    }

    @Override
    public void onConfigurationChanged(android.content.res.Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        // 清单里声明了 configChanges，旋转不会重建 Activity，这里手动重报
        reportDevice();
    }

    private void immersive() {
        View d = getWindow().getDecorView();
        d.setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) immersive();
    }

    @Override
    protected void onResume() {
        super.onResume();
        ViewerBridge.activityVisible = true;
        reportDevice();
    }

    @Override
    protected void onPause() {
        super.onPause();
        ViewerBridge.activityVisible = false;
    }

    @Override
    protected void onDestroy() {
        ViewerBridge.activityVisible = false;
        if (ViewerBridge.listener == urlListener) ViewerBridge.listener = null;
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
