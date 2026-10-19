// @vitest-environment node

import { afterEach, describe, expect, it, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import * as THREE from "three";
import URDFLoader, { type URDFRobot } from "urdf-loader";

import { applyTelemetryPose, derivePoseStatus } from "./G1Viewer";

const modelDirectory = new URL("../public/models/unitree-g1/", import.meta.url);
const modelContent = readFileSync(
  fileURLToPath(new URL("g1_29dof_rev_1_0.urdf", modelDirectory)),
  "utf8",
);

const profileJointNames = [
  "left_hip_pitch_joint",
  "left_hip_roll_joint",
  "left_hip_yaw_joint",
  "left_knee_joint",
  "left_ankle_pitch_joint",
  "left_ankle_roll_joint",
  "right_hip_pitch_joint",
  "right_hip_roll_joint",
  "right_hip_yaw_joint",
  "right_knee_joint",
  "right_ankle_pitch_joint",
  "right_ankle_roll_joint",
  "waist_yaw_joint",
  "waist_roll_joint",
  "waist_pitch_joint",
  "left_shoulder_pitch_joint",
  "left_shoulder_roll_joint",
  "left_shoulder_yaw_joint",
  "left_elbow_joint",
  "left_wrist_roll_joint",
  "left_wrist_pitch_joint",
  "left_wrist_yaw_joint",
  "right_shoulder_pitch_joint",
  "right_shoulder_roll_joint",
  "right_shoulder_yaw_joint",
  "right_elbow_joint",
  "right_wrist_roll_joint",
  "right_wrist_pitch_joint",
  "right_wrist_yaw_joint",
];

function parseOfficialModel(): URDFRobot {
  const dom = new JSDOM(modelContent, { contentType: "text/xml" });
  vi.stubGlobal("Document", dom.window.Document);
  vi.stubGlobal("Element", dom.window.Element);
  vi.stubGlobal("DOMParser", dom.window.DOMParser);
  const loader = new URDFLoader();
  loader.loadMeshCb = (_url, _manager, material, onLoad) => {
    onLoad(new THREE.Mesh(new THREE.BufferGeometry(), material));
  };
  return loader.parse(dom.window.document);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("local G1 URDF", () => {
  it("defines exactly the 29 profile-controlled joints", () => {
    const modelJointNames = [
      ...modelContent.matchAll(
        /<joint name="([^"]+)" type="(?:revolute|continuous|prismatic)"/g,
      ),
    ]
      .map((match) => match[1])
      .sort();

    expect(modelJointNames).toEqual([...profileJointNames].sort());
  });

  it("bundles every mesh referenced by the official URDF locally", () => {
    const meshes = [
      ...new Set(
        [...modelContent.matchAll(/filename="([^"]+\.stl)"/gi)].map(
          (match) => match[1],
        ),
      ),
    ];

    expect(meshes).toHaveLength(35);
    for (const mesh of meshes) {
      expect(existsSync(fileURLToPath(new URL(mesh, modelDirectory)))).toBe(true);
    }
  });

  it("maps every profile joint and applies measured joint and IMU pose", () => {
    const robot = parseOfficialModel();
    const telemetry = profileJointNames.map((urdf_joint_name, index) => ({
      urdf_joint_name,
      q_rad: index / 100,
    }));

    const result = applyTelemetryPose(robot, telemetry, [0.1, 0.2, 0.3]);

    expect(result).toEqual({
      modelJointCount: 29,
      mappedJointCount: 29,
      missingMappings: [],
      missingValues: 0,
      imuAvailable: true,
    });
    expect(robot.joints.left_hip_pitch_joint.jointValue[0]).toBe(0);
    expect(robot.joints.right_wrist_yaw_joint.jointValue[0]).toBe(0.28);
    expect(robot.rotation.order).toBe("ZYX");
    expect(robot.rotation.x).toBeCloseTo(0.1);
    expect(robot.rotation.y).toBeCloseTo(0.2);
    expect(robot.rotation.z).toBeCloseTo(0.3);
    expect(derivePoseStatus(result, "fresh", "fresh", 5)).toBe("ready");
    expect(derivePoseStatus(result, "fresh", "delayed", 5)).toBe("stale");
    expect(derivePoseStatus(result, "fresh", "fresh", 2)).toBe(
      "model-mismatch",
    );
  });

  it("reports missing profile mappings instead of silently inventing joints", () => {
    const robot = parseOfficialModel();
    const result = applyTelemetryPose(
      robot,
      [{ urdf_joint_name: "unknown_joint", q_rad: 0 }],
      null,
    );

    expect(result.modelJointCount).toBe(29);
    expect(result.mappedJointCount).toBe(0);
    expect(result.missingMappings).toEqual(["unknown_joint"]);
    expect(derivePoseStatus(result, "fresh", "fresh", 5)).toBe("mapping-error");
  });

  it("rejects duplicate telemetry mappings", () => {
    const robot = parseOfficialModel();
    const telemetry = profileJointNames.map((urdf_joint_name) => ({
      urdf_joint_name,
      q_rad: 0,
    }));
    telemetry[28] = telemetry[27];

    const result = applyTelemetryPose(robot, telemetry, [0, 0, 0]);

    expect(result.mappedJointCount).toBe(28);
    expect(result.missingMappings).toEqual(["right_wrist_pitch_joint"]);
    expect(derivePoseStatus(result, "fresh", "fresh", 5)).toBe("mapping-error");
  });
});
