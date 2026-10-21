#!/usr/bin/env bash
# 回放一次动作并打印逐关节跟踪误差、躯干姿态变化，以及腰的 mode 回读。
#
# 用法: bash ~/robot-gui-track.sh <动作名>   例如 p1_s2
#
# 采样器在仓库里（tools/pose_sampler.py），跟着代码一起部署，不再放在 /tmp
# —— 以前放 /tmp，机器人重启就没了，track.sh 直接跑不起来。
#
# 判读要点（详见 docs/deployment-g1.md §6.3）：
#   腰 12/13/14 的「模式」读数从 1 变成 0 → 我们写的 mode=0 真的作用到了驱动；
#   配合腰 14 的角度曲线判断固件是继续管腰（腰稳）还是把腰交给了我们（腰瘫）。
set -u

ACTION="${1:-p1_s2}"
ROOT=/usr/local/src/robot-gui
BASE=http://127.0.0.1:8000
PY=$ROOT/backend/.venv/bin/python
SAMPLER=$ROOT/tools/pose_sampler.py
SAMPLE_SECONDS=25
OUT=/tmp/pose_samples.json

# DDS 网卡名从后端的 env 文件里取，和后端自己用的一致
ENV_FILE=$HOME/.config/robot-gui/env.sh
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
IFACE=${ROBOT_GUI_DDS_INTERFACE:-eth0}

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

echo "=== 触发回放 $ACTION ==="
curl -sS --max-time 15 -X POST $BASE/api/v1/control/replay \
  -H 'Content-Type: application/json' \
  -d "{\"action\": \"$ACTION\", \"confirmed\": true}" | head -c 300
echo
echo "等待回放结束..."
for i in $(seq 1 120); do
  P=$(curl -sS --max-time 5 $BASE/api/v1/control/state 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['phase'])" 2>/dev/null)
  [ "$P" = "idle" ] || [ "$P" = "fault" ] && break
  sleep 0.5
done
sleep 2
echo
echo "=== 回放后 ==="
show_state
echo
echo "=== 下盘漂移（腰/腿偏离接管瞬间）==="
curl -sS --max-time 10 $BASE/api/v1/control/state | python3 -c "
import json,sys
lb=json.load(sys.stdin).get('lower_body') or {}
if not lb:
    print('  (无数据)'); raise SystemExit
print('  阈值 %.1f°  最大漂移 %.2f° @%s  腰14漂移 %.2f°  越限=%s' % (
    lb['threshold_deg'], lb['max_drift_deg'], lb['max_drift_joint'] or '-',
    lb['waist_pitch_drift_deg'], lb['violated']))
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
print('    腰 mode 回读 1→0 且腰 14 稳  → mode=0 是交回固件，方案成立')
print('    腰 mode 回读 1→0 且腰 14 瘫  → mode=0 是断力矩，立刻停机，方案不成立')
print('    腰 mode 回读 全程 1          → arm_sdk 不影响 per-motor mode，腰由固件管')
print('    对照跑（ROBOT_GUI_WAIST_SLOT_MODE=1）时腰瘫 → 固件根本不接管腰')
"
