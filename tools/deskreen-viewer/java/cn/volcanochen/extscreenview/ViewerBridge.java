package cn.volcanochen.extscreenview;

/**
 * 跨组件桥：ViewerService 发现投屏地址后写入，MainActivity 观察并加载。
 * Activity 重建（旋转/被回收后重开）也能立刻拿到最新地址。
 */
public final class ViewerBridge {
    public interface OnUrl {
        void onUrl(String url);
    }

    /** 最新的投屏地址 http://<pc>:<port>/<roomID>；未发现为 null。 */
    public static volatile String url;
    /**
     * 粘滞会话：已提交给 WebView 的地址。多路投屏场景下 PC 会同时广播
     * 多个 cast 服务，mDNS 发现不能「见活的服务就切」——否则手机 A/B
     * 会互相跳到对方的会话上（2026-11-05 实测：投 X 给 B，B 被 mDNS
     * 吸回已在投的 Y）。粘住的地址只有确认死了才释放。
     */
    public static volatile String pin;
    public static volatile boolean activityVisible;
    public static volatile OnUrl listener;

    private ViewerBridge() {}

    public static void setUrl(String u) {
        url = u;
        pin = u;   // 提交即粘住
        OnUrl l = listener;
        if (l != null) l.onUrl(u);
    }
}
