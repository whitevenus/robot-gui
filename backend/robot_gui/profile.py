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
    JointDescriptor("right_shoulder_pitch", "right_shoulder_pitch_joint", 22, "right_arm"),
    JointDescriptor("right_shoulder_roll", "right_shoulder_roll_joint", 23, "right_arm"),
    JointDescriptor("right_shoulder_yaw", "right_shoulder_yaw_joint", 24, "right_arm"),
    JointDescriptor("right_elbow", "right_elbow_joint", 25, "right_arm"),
    JointDescriptor("right_wrist_roll", "right_wrist_roll_joint", 26, "right_arm"),
    JointDescriptor("right_wrist_pitch", "right_wrist_pitch_joint", 27, "right_arm"),
    JointDescriptor("right_wrist_yaw", "right_wrist_yaw_joint", 28, "right_arm"),
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
