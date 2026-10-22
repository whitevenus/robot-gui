/** 深度相机实时画面。

后端把最新一帧以 MJPEG 推出来，浏览器直接用 `<img src>` 接，不用前端轮询。
取不到画面时照实写明原因，不用别的东西凑一张图。
*/
import { useEffect, useState } from "react";

interface CameraState {
  available: boolean;
  error_code: string;
  error_message: string;
  frames: number;
  frame_age_s: number | null;
}

export function DepthCameraView() {
  const [expanded, setExpanded] = useState(false);
  const [state, setState] = useState<CameraState | null>(null);

  useEffect(() => {
    let stopped = false;
    const read = async () => {
      try {
        const response = await fetch("/api/v1/camera/state");
        if (response.ok) {
          const body = (await response.json()) as CameraState;
          if (!stopped) setState(body);
        }
      } catch {
        // 后端不可达时保留上一次状态，不伪造可用。
      }
    };
    void read();
    const timer = window.setInterval(() => {
      void read();
    }, 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  const available = state?.available === true;
  const reason = state?.error_message || "正在连接深度相机…";

  return (
    <section
      className={`card camera-card${expanded ? " expanded" : ""}`}
      aria-label="深度相机实时画面"
    >
      <div className="card-head">
        <h2>深度相机</h2>
        <div className="card-head-actions">
          <span
            className={`src-state ${
              state === null ? "state-unknown" : available ? "state-fresh" : "state-offline"
            }`}
          >
            {state === null ? "连接中" : available ? "实时" : "不可用"}
          </span>
          <button
            type="button"
            className="view-reset"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "缩小" : "放大"}
          </button>
        </div>
      </div>
      <div className="camera-view">
        {available ? (
          <img src="/api/v1/camera/stream" alt="深度相机实时画面" />
        ) : (
          <p className="camera-empty">{reason}</p>
        )}
      </div>
    </section>
  );
}
