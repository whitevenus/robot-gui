#!/usr/bin/env bash
# 回放一次动作（或课程清单里的**一整套**），打印逐关节跟踪误差、躯干姿态变化，
# 以及腰的 mode 回读。
#
# 用法: ./tools/robot-gui-track.sh <动作名>   例如 p1_s2
#       ./tools/robot-gui-track.sh course      播整套（按 data/course.json 的顺序）
#
# 脚本和采样器都在仓库的 tools/ 下，跟着代码一起部署，不往家目录散落。
# 采样数据统一写仓库的 logs/ 目录。
#
# 判读要点（详见 docs/deployment-g1.md §6.2）：
#   腰 14 的前倾峰值：< 5° 说明腰的支撑力矩够用；顶到 +30° 说明力矩饱和或增益没生效。
set -u

ACTION="${1:-p1_s2}"
# 从脚本自身位置推仓库根，不写死绝对路径
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BASE=http://127.0.0.1:8000
PY=$ROOT/backend/.venv/bin/python
SAMPLER=$ROOT/tools/pose_sampler.py
LOGS=$ROOT/logs
mkdir -p "$LOGS"
OUT=$LOGS/pose_samples.json

# DDS 网卡名从项目内的 config/env.sh 取，和后端自己用的一致
ENV_FILE=$ROOT/config/env.sh
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
IFACE=${ROBOT_GUI_DDS_INTERFACE:-eth0}
DATA_DIR=${ROBOT_GUI_DATA_DIR:-$ROOT/data}

# 整套 = 课程清单里的所有动作连成一条轨迹；采样窗口要覆盖全程（含沉降期），
# 否则前倾峰值会被漏掉（它是延迟发展的，见 docs §6.1）。
if [ "$ACTION" = "course" ]; then
  # 每段动作之间一段 2 秒衔接（进入 + 段间 + 返回），再加 3 秒沉降和 10 秒余量。
  SAMPLE_SECONDS=$($PY -c "
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
c = json.loads((d / 'course.json').read_text(encoding='utf-8'))
motion = sum(float(json.loads((d / (n + '.json')).read_text(encoding='utf-8'))['frames'][-1]['t']) for n in c['actions'])
print(int(motion + 2.0 * (len(c['actions']) + 1) + 13.0))
" "$DATA_DIR")
  REPLAY_BODY='{"course": true, "confirmed": true}'
  LABEL="整套 $($PY -c "
import json, pathlib, sys
c = json.loads((pathlib.Path(sys.argv[1]) / 'course.json').read_text(encoding='utf-8'))
print(str(len(c['actions'])) + ' 段')
" "$DATA_DIR")"
else
  SAMPLE_SECONDS=${ROBOT_GUI_SAMPLE_SECONDS:-25}
  REPLAY_BODY="{\"action\": \"$ACTION\", \"confirmed\": true}"
  LABEL="$ACTION"
fi

show_state() {
  curl -sS --max-time 10 $BASE/api/v1/status | python3 -c "
import json,sys
s=json.load(sys.stdin); r=s['robot']
j={x['name']: x['q_rad'] for x in r['joints']}
m={x['name']: x['mode'] for x in r['joints']}
print('  腰  yaw/roll/pitch : %+7.4f %+7.4f %+7.4f' % (j['waist_yaw'], j['waist_roll'], j['waist_pitch']))
print('  腰  mode 回读      : %s %s %s' % (m['waist_yaw'], m['waist_roll'], m['waist_pitch']))
print('  躯干IMU rpy        : %s' % [round(x,4) for x in (r['torso_imu'] or {}).get('rpy',[])])
print('  髋部IMU rpy        : %s' % [round(x,4) for x in (r['hip_imu'] or {}).get('rpy',[])])
"
}

if [ ! -f "$SAMPLER" ]; then
  echo "缺少采样器：$SAMPLER（仓库 tools/ 目录没部署过来？）" >&2
  exit 1
fi

echo "=== 回放前 ==="
show_state
echo

# 全程姿态采样：漂移是延迟出现的，只采回放前后会漏掉峰值。
# 采样器与回放并行启动，结束后输出时间序列。
$PY "$SAMPLER" "$IFACE" "$SAMPLE_SECONDS" --out "$OUT" &
SAMPLER_PID=$!

echo "=== 触发回放 $LABEL ==="
curl -sS --max-time 15 -X POST $BASE/api/v1/control/replay \
  -H 'Content-Type: application/json' \
  -d "$REPLAY_BODY" | head -c 300
echo
echo "等待回放结束..."
# 等待上限跟着采样窗口走：整套要跑一分多钟，60 秒不够。
for i in $(seq 1 $((SAMPLE_SECONDS * 2))); do
  P=$(curl -sS --max-time 5 $BASE/api/v1/control/state 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['phase'])" 2>/dev/null)
  [ "$P" = "idle" ] || [ "$P" = "fault" ] && break
  sleep 0.5
done
sleep 2
echo
echo "=== 回放后 ==="
show_state
echo
echo "=== 下盘守卫（腿看相对偏离，腰 14 看绝对前倾）==="
curl -sS --max-time 10 $BASE/api/v1/control/state | python3 -c "
import json,sys
lb=json.load(sys.stdin).get('lower_body') or {}
if not lb:
    print('  (无数据)'); raise SystemExit
print('  阈值 %.1f°  腿/腰最大偏离 %.2f° @%s  腰14前倾峰值 %.2f°  腰14回位 %.2f°  越限=%s' % (
    lb['threshold_deg'], lb['max_drift_deg'], lb['max_drift_joint'] or '-',
    lb.get('waist_pitch_lean_deg', 0.0), lb['waist_pitch_drift_deg'], lb['violated']))
"
echo
echo "=== 逐关节跟踪误差（命令目标 vs 实测）==="
curl -sS --max-time 10 $BASE/api/v1/control/state | python3 -c "
import json,sys
t=json.load(sys.stdin).get('tracking') or {}
if not t:
    print('  (没有采样到跟踪数据)'); raise SystemExit
print('  采样点:', t.get('samples'))
rows=[(k,v) for k,v in t['joints'].items()]
rows.sort(key=lambda kv: -kv[1]['max_error_rad'])
print('  %-24s %9s %9s %9s %9s' % ('关节','最大偏差','平均偏差','目标角','实测角'))
for k,v in rows:
    print('  %-24s %9.4f %9.4f %9.3f %9.3f' % (k, v['max_error_rad'], v['mean_error_rad'], v['target_rad'], v['measured_rad']))
"
echo
wait $SAMPLER_PID 2>/dev/null
echo
echo "=== 腰 14 全程时间序列（每 0.5 秒）==="
python3 -c "
import json, math
try:
    s=json.load(open('$OUT'))
except Exception as e:
    print('  (无采样数据)', e); raise SystemExit
if not s:
    print('  (无采样数据)'); raise SystemExit
print('  %6s %12s %12s %12s %10s' % ('t(s)','腰14pitch(度)','躯干pitch','髋部pitch','腰mode'))
for row in s[::10]:
    def d(x): return '%+12.2f' % math.degrees(x) if x is not None else '%12s' % '-'
    modes = row.get('waist_modes') or []
    print('  %6.2f %s %s %s %10s' % (row['t'], d(row['waist_pitch']), d(row['torso_pitch']), d(row['hip_pitch']), ','.join(str(m) for m in modes)))
ps=[r['waist_pitch'] for r in s if r['waist_pitch'] is not None]
if ps:
    print('  => 腰14 峰值前倾 %.2f°，全程跨度 %.2f°' % (
        math.degrees(max(ps)), math.degrees(max(ps)-min(ps))))
"
echo
echo "=== 腰的 mode 回读 与 我们发出的报文 ==="
python3 -c "
import json
try:
    s=json.load(open('$OUT'))
except Exception as e:
    print('  (无采样数据)', e); raise SystemExit
if not s:
    print('  (无采样数据)'); raise SystemExit

modes = {}
for row in s:
    for m in (row.get('waist_modes') or []):
        modes[m] = modes.get(m, 0) + 1
print('  腰 mode 回读取值分布 :', dict(sorted(modes.items(), key=lambda kv: str(kv[0]))))

changes = []
last = None
for row in s:
    m = tuple(row.get('waist_modes') or [])
    if m and m != last:
        changes.append((row['t'], m))
        last = m
if changes:
    print('  腰 mode 变化时刻     :')
    for t, m in changes:
        print('      t=%6.2fs -> %s' % (t, list(m)))
else:
    print('  腰 mode 全程未变化')

weights = [r['cmd_weight'] for r in s if r.get('cmd_weight') is not None]
if weights:
    print('  我们发的接管权重     : 最小 %.2f 最大 %.2f（0=未接管 1=完全接管）' % (min(weights), max(weights)))
sent = [r['cmd_waist_mode'] for r in s if r.get('cmd_waist_mode') is not None]
if sent:
    print('  我们发的腰槽位 mode  :', sorted(set(sent)))
print()
print('  判读：')
print('    腰14 前倾峰值 < 5° 且稳   → 腰的支撑力矩够用，方案成立（守卫同口径）')
print('    腰14 前倾峰值 5~15°      → 支撑力矩不够，按 50 Nm 力矩上限往上调 kp')
print('    腰14 前倾峰值 顶到 +30° 附近 → 力矩饱和或增益没生效，查腰的 kp/kd 与 tau')
print('    腰 mode 回读全程 1        → 正常，per-motor mode 不影响腰的行为')
"
