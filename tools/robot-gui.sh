#!/usr/bin/env bash
# Robot GUI 真机侧启停脚本。
#
# 放在仓库里（tools/robot-gui.sh），家目录只留一个软链：
#     ln -sf /usr/local/src/robot-gui/tools/robot-gui.sh ~/robot-gui.sh
# 这样脚本本体跟着代码版本走，配置和脚本都在一处，方便查看和管理。
set -u

# 从脚本自身位置推仓库根，不写死绝对路径
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE=$HOME/.config/robot-gui/env.sh
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
export PATH=$HOME/.local/bin:$PATH

start_backend() {
  cd "$ROOT"
  setsid nohup uv run --directory backend uvicorn robot_gui.main:app \
      --host 0.0.0.0 --port 8000 > /tmp/robot-gui-backend.log 2>&1 < /dev/null &
  echo "backend -> /tmp/robot-gui-backend.log"
}
start_frontend() {
  cd "$ROOT/frontend"
  setsid nohup npm run dev -- --host 0.0.0.0 --port 5173 \
      > /tmp/robot-gui-frontend.log 2>&1 < /dev/null &
  echo "frontend -> /tmp/robot-gui-frontend.log"
}
stop_all() {
  pkill -f "uvicorn robot_gui" 2>/dev/null && echo "stopped backend" || echo "backend 未运行"
  pkill -f "vite" 2>/dev/null && echo "stopped frontend" || echo "frontend 未运行"
}
status() {
  echo "--- 进程 ---"
  ps aux | grep -E "[u]vicorn robot_gui|[v]ite" | awk "{print \$2, \$11, \$12, \$13, \$14, \$15}"
  echo "--- 端口 ---"
  (ss -tlnp 2>/dev/null || netstat -tlnp) | grep -E "8000|5173" || echo "(无监听)"
  echo "--- 后端状态 ---"
  curl -sS --max-time 5 http://127.0.0.1:8000/api/v1/status 2>/dev/null | python3 -c "
import json,sys
try:
    s=json.load(sys.stdin)
    print('  connection:', s['connection']['state'], '| FSM', (s['robot']['fsm'] or {}).get('id'))
    print('  sources   :', {k: v['state'] for k,v in s['sources'].items()})
except Exception as e:
    print('  (无响应)', e)
" 2>/dev/null || echo "  (后端无响应)"
}
case "${1:-status}" in
  start) start_backend; sleep 3; start_frontend ;;
  start-control)
    # 显式开启真机控制与机器人扬声器语音
    export ROBOT_GUI_CONTROLS_ENABLED=1
    export ROBOT_GUI_FSM_WHITELIST=501
    export ROBOT_GUI_AUDIO_ENABLED=1
    start_backend; sleep 3; start_frontend ;;
  start-backend) start_backend ;;
  start-frontend) start_frontend ;;
  stop) stop_all ;;
  restart) stop_all; sleep 2; start_backend; sleep 3; start_frontend ;;
  restart-control)
    export ROBOT_GUI_CONTROLS_ENABLED=1
    export ROBOT_GUI_FSM_WHITELIST=501
    export ROBOT_GUI_AUDIO_ENABLED=1
    stop_all; sleep 2; start_backend; sleep 3; start_frontend ;;
  status) status ;;
  *) echo "用法: $0 {start|stop|restart|status}"; exit 1 ;;
esac
