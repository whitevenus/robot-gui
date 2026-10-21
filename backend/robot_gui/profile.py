from dataclasses import dataclass


@dataclass(frozen=True)
class JointDescriptor:
    name: str
    urdf_joint_name: str
    sdk_slot: int
    group: str


G1_29DOF_JOINTS = (
    JointDescriptor("left_hip_pitch", "left_hip_pitch_joint", 0, "left_leg"),
    JointDescriptor("left_hip_roll", "left_hip_roll_joint", 1, "left_leg"),
    JointDescriptor("left_hip_yaw", "left_hip_yaw_joint", 2, "left_leg"),
    JointDescriptor("left_knee", "left_knee_joint", 3, "left_leg"),
    JointDescriptor("left_ankle_pitch", "left_ankle_pitch_joint", 4, "left_leg"),
    JointDescriptor("left_ankle_roll", "left_ankle_roll_joint", 5, "left_leg"),
    JointDescriptor("right_hip_pitch", "right_hip_pitch_joint", 6, "right_leg"),
    JointDescriptor("right_hip_roll", "right_hip_roll_joint", 7, "right_leg"),
    JointDescriptor("right_hip_yaw", "right_hip_yaw_joint", 8, "right_leg"),
    JointDescriptor("right_knee", "right_knee_joint", 9, "right_leg"),
    JointDescriptor("right_ankle_pitch", "right_ankle_pitch_joint", 10, "right_leg"),
    JointDescriptor("right_ankle_roll", "right_ankle_roll_joint", 11, "right_leg"),
    JointDescriptor("waist_yaw", "waist_yaw_joint", 12, "waist"),
    JointDescriptor("waist_roll", "waist_roll_joint", 13, "waist"),
    JointDescriptor("waist_pitch", "waist_pitch_joint", 14, "waist"),
    JointDescriptor("left_shoulder_pitch", "left_shoulder_pitch_joint", 15, "left_arm"),
    JointDescriptor("left_shoulder_roll", "left_shoulder_roll_joint", 16, "left_arm"),
    JointDescriptor("left_shoulder_yaw", "left_shoulder_yaw_joint", 17, "left_arm"),
    JointDescriptor("left_elbow", "left_elbow_joint", 18, "left_arm"),
    JointDescriptor("left_wrist_roll", "left_wrist_roll_joint", 19, "left_arm"),
    JointDescriptor("left_wrist_pitch", "left_wrist_pitch_joint", 20, "left_arm"),
    JointDescriptor("left_wrist_yaw", "left_wrist_yaw_joint", 21, "left_arm"),
    JointDescriptor(
        "right_shoulder_pitch", "right_shoulder_pitch_joint", 22, "right_arm"
    ),
    JointDescriptor(
        "right_shoulder_roll", "right_shoulder_roll_joint", 23, "right_arm"
    ),
    JointDescriptor("right_shoulder_yaw", "right_shoulder_yaw_joint", 24, "right_arm"),
    JointDescriptor("right_elbow", "right_elbow_joint", 25, "right_arm"),
    JointDescriptor("right_wrist_roll", "right_wrist_roll_joint", 26, "right_arm"),
    JointDescriptor("right_wrist_pitch", "right_wrist_pitch_joint", 27, "right_arm"),
    JointDescriptor("right_wrist_yaw", "right_wrist_yaw_joint", 28, "right_arm"),
)

#: 本工作台的接管范围：**双臂 14 关节 + 腰 3 关节的姿态保持**。
#: 腿（slot 0-11）不接管，由机器人内置运控负责。
#: 腰虽然不由动作驱动（动作是 arms 组 14 维，腰的目标角恒等于接管瞬间的姿态），
#: 但**必须由我们下发指令**：arm_sdk 的权重一旦升到 1，腰的控制权就无条件归我们，
#: per-motor mode 写 0 或 1 都没有区别（2026-10-21 实测两档前倾峰值 34.01° vs
#: 34.15°，曲线形状一致）。腰槽位留空（增益全 0）时，腰在接管后 0.8 秒内被推到
#: +32.8° 的机械限位，固件的腰部平衡回路退让，躯干前倾 +40°。
ARM_GROUPS = ("left_arm", "right_arm")
#: 下盘 = 腿 + 腰，漂移守卫只看这一组；手臂在动作里本来就在动，不算漂移。
LOWER_BODY_GROUPS = ("left_leg", "right_leg", "waist")

#: 被接管的 SDK 槽位（双臂 14 个）。
ARM_SDK_SLOTS: tuple[int, ...] = tuple(
    joint.sdk_slot for joint in G1_29DOF_JOINTS if joint.group in ARM_GROUPS
)
#: 不接管的腰部槽位。
WAIST_SDK_SLOTS: tuple[int, ...] = tuple(
    joint.sdk_slot for joint in G1_29DOF_JOINTS if joint.group == "waist"
)
#: 漂移守卫关注的关节名（腿 + 腰）。
LOWER_BODY_JOINT_NAMES: frozenset[str] = frozenset(
    joint.name for joint in G1_29DOF_JOINTS if joint.group in LOWER_BODY_GROUPS
)
#: 腰 3 关节的名字，按 SDK 槽位顺序（yaw 12 / roll 13 / pitch 14）。
WAIST_JOINT_NAMES: tuple[str, ...] = tuple(
    joint.name for joint in G1_29DOF_JOINTS if joint.group == "waist"
)

if len(G1_29DOF_JOINTS) != 29:
    raise RuntimeError("G1 29DoF profile must define exactly 29 joints.")
if len({joint.name for joint in G1_29DOF_JOINTS}) != 29:
    raise RuntimeError("G1 joint names must be unique.")
if len({joint.sdk_slot for joint in G1_29DOF_JOINTS}) != 29:
    raise RuntimeError("G1 SDK slots must be unique.")
if {joint.sdk_slot for joint in G1_29DOF_JOINTS} != set(range(29)):
    raise RuntimeError("G1 29DoF SDK slots must cover 0 through 28.")
if len({joint.urdf_joint_name for joint in G1_29DOF_JOINTS}) != 29:
    raise RuntimeError("G1 URDF joint names must be unique.")
