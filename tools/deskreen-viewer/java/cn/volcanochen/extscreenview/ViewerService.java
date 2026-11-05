package cn.volcanochen.extscreenview;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import java.net.InetAddress;
import java.util.Map;

/**
 * 前台服务：mDNS 发现 PC 端广播的 _extscreen-deskreen._tcp 服务，
 * 把 http://<pc>:<port>/<roomID> 写进 ViewerBridge。
 *
 * 关键设计：
 *  - 周期性重新发现（20s）：房间号轮换后 TXT 会变，必须重新解析才能拿到新 path；
 *  - NsdManager 解析期间会暂停发现，解析完（无论成败）都重新拉起发现；
 *  - 首次发现新地址时发一条全屏 Intent 通知，把界面顶到前台（开机后免手点）。
 */
public class ViewerService extends Service {

    /** 同时发现两种服务：自研 cast 后端 + Deskreen CE（历史兼容）。 */
    private static final String[] TYPES = {"_extscreen-cast", "_extscreen-deskreen"};
    private static final long REFOUND_INTERVAL_MS = 8_000;

    private NsdManager nsd;
    private final Handler h = new Handler(Looper.getMainLooper());
    private final java.util.Map<String, NsdManager.DiscoveryListener> discs =
            new java.util.HashMap<>();
    private boolean resolving;
    private int discoverFails;
    private String lastAnnouncedUrl;
    /** 验证过已死的发现结果 → 拉黑截止时间（避免每 8 秒反复撞死链）。 */
    private final java.util.Map<String, Long> deadUntil = new java.util.HashMap<>();
    private static final long DEAD_MS = 5 * 60_000;

    private final Runnable refoundRun = new Runnable() {
        @Override public void run() { startDiscover(); h.postDelayed(this, REFOUND_INTERVAL_MS); }
    };

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startFg();
        if (nsd == null) nsd = (NsdManager) getSystemService(NSD_SERVICE);
        h.removeCallbacks(refoundRun);
        startDiscover();
        h.postDelayed(refoundRun, REFOUND_INTERVAL_MS);

        // 调试通道：adb shell am start-foregroundservice ... --es debug_url http://...
        // 用于在 mDNS 不便的环境（沙箱/防火墙）里单独验证 WebView 链路
        if (intent != null && intent.getStringExtra("debug_url") != null) {
            String u = intent.getStringExtra("debug_url");
            ViewerBridge.setUrl(u);
            lastAnnouncedUrl = u;
            return START_STICKY;
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        h.removeCallbacks(refoundRun);
        stopDiscover();
        super.onDestroy();
    }

    // ------------------------------------------------------------------
    // NSD
    // ------------------------------------------------------------------
    private void stopDiscover() {
        for (NsdManager.DiscoveryListener d : discs.values()) {
            try { nsd.stopServiceDiscovery(d); } catch (Exception ignored) {}
        }
        discs.clear();
    }

    private void startDiscover() {
        if (nsd == null) return;
        stopDiscover();
        for (final String type : TYPES) {
            NsdManager.DiscoveryListener d = new NsdManager.DiscoveryListener() {
                @Override public void onDiscoveryStarted(String t) { discoverFails = 0; }
                @Override public void onStartDiscoveryFailed(String t, int e) {
                    discoverFails++;
                    long delay = Math.min(30_000, 2_000L * discoverFails);
                    h.postDelayed(new Runnable() {
                        @Override public void run() { startDiscover(); }
                    }, delay);
                }
                @Override public void onServiceFound(NsdServiceInfo info) {
                    String t = info.getServiceType();
                    if (t != null && !resolving) {
                        resolve(info);
                    }
                }
                @Override public void onServiceLost(NsdServiceInfo info) { /* 保留当前画面 */ }
                @Override public void onDiscoveryStopped(String t) { }
                @Override public void onStopDiscoveryFailed(String t, int e) { }
            };
            discs.put(type, d);
            try {
                nsd.discoverServices(type + "._tcp.", NsdManager.PROTOCOL_DNS_SD, d);
            } catch (Exception e) {
                h.postDelayed(new Runnable() {
                    @Override public void run() { startDiscover(); }
                }, 5_000);
                return;
            }
        }
    }

    private void resolve(final NsdServiceInfo info) {
        resolving = true;
        NsdManager.ResolveListener l = new NsdManager.ResolveListener() {
            @Override public void onServiceResolved(NsdServiceInfo r) {
                resolving = false;
                apply(r);
                h.postDelayed(new Runnable() {
                    @Override public void run() { startDiscover(); }
                }, 1_500);
            }
            @Override public void onResolveFailed(NsdServiceInfo i, int errorCode) {
                resolving = false;
                h.postDelayed(new Runnable() {
                    @Override public void run() { startDiscover(); }
                }, 3_000);
            }
        };
        try {
            nsd.resolveService(info, l);
        } catch (Exception e) {
            resolving = false;
        }
    }

    private void apply(NsdServiceInfo r) {
        String path = "/";
        Map<String, byte[]> attrs = r.getAttributes();
        if (attrs != null) {
            byte[] p = attrs.get("path");
            if (p != null && p.length > 0) path = new String(p).trim();
        }
        InetAddress host = r.getHost();
        if (host == null || r.getPort() <= 0) return;
        final String url = "http://" + host.getHostAddress() + ":" + r.getPort() + path;

        // 网络判断不能在主线程做。粘滞判定也在这里一并完成：
        // 已粘住的会话还活着 → 无视一切新发现（多路投屏防抢台，
        // 2026-11-05 实测：不粘的话手机会被 mDNS 吸到别的会话上）；
        // 粘住的会话死了 → 释放粘滞，转投发现的活服务（Dock 重启自愈靠它）。
        new Thread(new Runnable() {
            @Override public void run() {
                String p = ViewerBridge.pin;
                long now = System.currentTimeMillis();
                if (p != null) {
                    if (urlAlive(p)) return;
                    deadUntil.put(p, now + DEAD_MS);
                    ViewerBridge.pin = null;   // 粘的死了，放手
                }
                // 已在用的 URL 不动（页面靠 img 断流重连自愈，无需重载）
                if (url.equals(ViewerBridge.url)) return;
                // 近期验证过死链的发现结果直接忽略（Dock 重启前的陈旧实例：
                // 端口已死，贸然切换会把好好的页面覆盖成错误页——实测踩过）
                Long dead = deadUntil.get(url);
                if (dead != null) {
                    if (dead > now) return;
                    deadUntil.remove(url);
                }
                // 后台健康检查，活的才切换
                if (!urlAlive(url)) {
                    deadUntil.put(url, System.currentTimeMillis() + DEAD_MS);
                    return;
                }
                h.post(new Runnable() {
                    @Override public void run() {
                        ViewerBridge.setUrl(url);
                        if (!url.equals(lastAnnouncedUrl)) {
                            lastAnnouncedUrl = url;
                            notifyFullScreen();
                        }
                    }
                });
            }
        }, "viewer-health-check").start();
    }

    /** 轻量探活：GET 一下看是不是 200。 */
    private static boolean urlAlive(String url) {
        try {
            java.net.HttpURLConnection c = (java.net.HttpURLConnection)
                    new java.net.URL(url).openConnection();
            c.setConnectTimeout(1200);
            c.setReadTimeout(1500);
            c.setRequestMethod("GET");
            int code = c.getResponseCode();
            java.io.InputStream is = c.getInputStream();
            byte[] b = new byte[64];
            is.read(b);   // 读一小口确认真的有响应体
            is.close();
            c.disconnect();
            return code == 200;
        } catch (Exception e) {
            return false;
        }
    }

    // ------------------------------------------------------------------
    // 通知
    // ------------------------------------------------------------------
    private void startFg() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(
                "viewer", "副屏接收", NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(ch);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, "viewer")
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.ic_media_play)
         .setContentTitle("副屏接收器运行中")
         .setContentText("正在局域网内寻找副屏主机…")
         .setOngoing(true)
         .setContentIntent(PendingIntent.getActivity(this, 0,
                 new Intent(this, MainActivity.class),
                 PendingIntent.FLAG_IMMUTABLE));
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, b.build(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(1, b.build());
        }
    }

    private void notifyFullScreen() {
        if (ViewerBridge.activityVisible) return;
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(
                "alert", "投屏提醒", NotificationManager.IMPORTANCE_HIGH);
        nm.createNotificationChannel(ch);
        PendingIntent pi = PendingIntent.getActivity(this, 1,
                new Intent(this, MainActivity.class)
                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, "alert")
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.ic_media_play)
         .setContentTitle("发现副屏主机")
         .setContentText("点击打开投屏画面")
         .setAutoCancel(true)
         .setFullScreenIntent(pi, true);
        nm.notify(2, b.build());
    }
}
