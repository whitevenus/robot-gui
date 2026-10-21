# Robot GUI 机器人侧配置。放在项目目录里，跟着代码一起走，不往家目录散落。
#
# 由 tools/robot-gui.sh 自动 source；也可以手动：
#   cd /usr/local/src/robot-gui && . ./config/env.sh

# 从本文件位置推项目根，不写死绝对路径
ROBOT_GUI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

export ROBOT_GUI_MODE=real
export ROBOT_GUI_DDS_INTERFACE=eth0
export ROBOT_GUI_TELEMETRY_HZ=10

# 动作库（导入的 group+frames JSON 与 audio/*.pcm）
export ROBOT_GUI_DATA_DIR="$ROBOT_GUI_ROOT/data"

# 增益配置：arms 与 waist 两组都要——腰的槽位由我们下发，缺了刚度躯干会前倾。
export ROBOT_GUI_GAINS="$ROBOT_GUI_ROOT/config/gains.json"

export PATH=$HOME/.local/bin:$PATH
