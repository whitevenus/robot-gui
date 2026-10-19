import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import URDFLoader, { type URDFRobot } from "urdf-loader";

const modelUrl = "/models/unitree-g1/g1_29dof_rev_1_0.urdf";

export interface ViewerJoint {
  urdf_joint_name: string;
  q_rad: number | null;
}

interface G1ViewerProps {
  joints: ViewerJoint[];
  hipImuRpy: number[] | null;
  hipImuAgeMs: number | null;
  hipImuState: string;
  lowStateAgeMs: number | null;
  lowStateState: string;
  modeMachine: number | null;
}

interface PoseApplication {
  modelJointCount: number;
  mappedJointCount: number;
  missingMappings: string[];
  missingValues: number;
  imuAvailable: boolean;
}

const EXPECTED_JOINT_COUNT = 29;

export function derivePoseStatus(
  application: PoseApplication,
  lowStateState: string,
  hipImuState: string,
  modeMachine: number | null,
): string {
  if (
    application.modelJointCount !== EXPECTED_JOINT_COUNT ||
    application.missingMappings.length > 0
  ) {
    return "mapping-error";
  }
  if (modeMachine !== 5) return "model-mismatch";
  if (
    application.mappedJointCount !== EXPECTED_JOINT_COUNT ||
    application.missingValues > 0 ||
    !application.imuAvailable ||
    lowStateState !== "fresh" ||
    hipImuState !== "fresh"
  ) {
    return "stale";
  }
  return "ready";
}

export function applyTelemetryPose(
  robot: URDFRobot,
  joints: ViewerJoint[],
  hipImuRpy: number[] | null,
): PoseApplication {
  const modelJointNames = Object.keys(robot.joints).filter(
    (name) => robot.joints[name].jointType !== "fixed",
  );
  const missingMappings: string[] = [];
  const seenJointNames = new Set<string>();
  let mappedJointCount = 0;
  let missingValues = 0;

  for (const joint of joints) {
    if (seenJointNames.has(joint.urdf_joint_name)) {
      missingMappings.push(joint.urdf_joint_name);
      continue;
    }
    seenJointNames.add(joint.urdf_joint_name);
    const modelJoint = robot.joints[joint.urdf_joint_name];
    if (!modelJoint || modelJoint.jointType === "fixed") {
      missingMappings.push(joint.urdf_joint_name);
      continue;
    }
    mappedJointCount += 1;
    modelJoint.ignoreLimits = true;
    if (joint.q_rad === null || !Number.isFinite(joint.q_rad)) {
      missingValues += 1;
      continue;
    }
    robot.setJointValue(joint.urdf_joint_name, joint.q_rad);
  }

  const imuAvailable =
    hipImuRpy?.length === 3 &&
    hipImuRpy.every((value) => Number.isFinite(value));
  if (imuAvailable) {
    robot.rotation.set(hipImuRpy[0], hipImuRpy[1], hipImuRpy[2], "ZYX");
  }

  return {
    modelJointCount: modelJointNames.length,
    mappedJointCount,
    missingMappings,
    missingValues,
    imuAvailable,
  };
}

function disposeRobot(robot: URDFRobot): void {
  robot.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    mesh.geometry.dispose();
    const materials = Array.isArray(mesh.material)
      ? mesh.material
      : [mesh.material];
    materials.forEach((material) => material.dispose());
  });
}

function statusMessage(
  status: string,
  application: PoseApplication | null,
  lowStateState: string,
  hipImuState: string,
  modeMachine: number | null,
): string {
  if (status === "loading") return "正在加载 Unitree 官方 G1 29DoF URDF…";
  if (status === "load-error") return "官方本地模型或 WebGL 加载失败；关节表仍可使用。";
  if (!application) return "等待模型初始化。";
  if (
    application.modelJointCount !== EXPECTED_JOINT_COUNT ||
    application.missingMappings.length > 0
  ) {
    return `模型与产品 Profile 不匹配：已映射 ${application.mappedJointCount}/${EXPECTED_JOINT_COUNT} 个关节。`;
  }
  if (status === "model-mismatch") {
    return `机器人 mode_machine=${modeMachine ?? "未知"}；当前 rev 1.0 模型要求 mode_machine=5，姿态兼容性未确认。`;
  }
  if (
    application.mappedJointCount !== EXPECTED_JOINT_COUNT ||
    application.missingValues > 0 ||
    !application.imuAvailable
  ) {
    return "遥测关节值或 IMU 姿态不完整；模型不代表完整实测姿态。";
  }
  if (lowStateState !== "fresh" || hipImuState !== "fresh") {
    return `LowState ${lowStateState} / IMU ${hipImuState}；模型标记为 stale，保留最近可用姿态。`;
  }
  return `模型已就绪 · ${application.mappedJointCount}/${EXPECTED_JOINT_COUNT} 关节映射 · 实测姿态（非物理仿真）`;
}

export function G1Viewer({
  joints,
  hipImuRpy,
  hipImuAgeMs,
  hipImuState,
  lowStateAgeMs,
  lowStateState,
  modeMachine,
}: G1ViewerProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const robotRef = useRef<URDFRobot | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const latestPropsRef = useRef({
    joints,
    hipImuRpy,
    lowStateState,
    hipImuState,
    modeMachine,
  });
  const [status, setStatus] = useState("loading");
  const [application, setApplication] = useState<PoseApplication | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  latestPropsRef.current = {
    joints,
    hipImuRpy,
    lowStateState,
    hipImuState,
    modeMachine,
  };

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch (error) {
      setStatus("load-error");
      setLoadError(
        error instanceof Error ? error.message : "WebGL 初始化失败。",
      );
      return;
    }

    let disposed = false;
    let robot: URDFRobot | null = null;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100);
    camera.up.set(0, 0, 1);
    camera.position.set(2.5, -2.5, 1.8);
    scene.add(new THREE.HemisphereLight(0xdcecff, 0x253141, 2.2));
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.5);
    keyLight.position.set(2, -3, 4);
    scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0x75c8ed, 1.1);
    fillLight.position.set(-3, 2, 2);
    scene.add(fillLight);
    const ground = new THREE.GridHelper(3, 20, 0x43627a, 0x263b4c);
    ground.rotation.x = Math.PI / 2;
    ground.position.z = -0.9;
    scene.add(ground);

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x0b121b, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.target.set(0, 0, 0.25);
    controls.update();
    controls.saveState();
    controlsRef.current = controls;

    const resize = () => {
      if (!host.isConnected) return;
      const width = Math.max(1, host.clientWidth);
      const height = Math.max(1, host.clientHeight);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(host);
    resize();

    const updatePose = (model: URDFRobot) => {
      const current = latestPropsRef.current;
      const next = applyTelemetryPose(
        model,
        current.joints,
        current.hipImuRpy,
      );
      setApplication(next);
      setStatus(
        derivePoseStatus(
          next,
          current.lowStateState,
          current.hipImuState,
          current.modeMachine,
        ),
      );
    };

    const loadErrors: string[] = [];
    let pendingMeshLoads = 0;
    let robotParsed = false;
    const fitCameraToRobot = () => {
      if (disposed || !robotParsed || pendingMeshLoads > 0 || !robot) return;
      if (loadErrors.length > 0) {
        setStatus("load-error");
        setLoadError(`${loadErrors.length} 个本地模型资源加载失败。`);
        return;
      }
      const bounds = new THREE.Box3().setFromObject(robot);
      if (bounds.isEmpty()) {
        setStatus("load-error");
        setLoadError("官方 URDF 未加载到有效模型网格。");
        return;
      }
      const center = bounds.getCenter(new THREE.Vector3());
      const size = bounds.getSize(new THREE.Vector3());
      const distance = Math.max(size.x, size.y, size.z) * 1.9;
      controls.target.copy(center);
      camera.position.set(
        center.x + distance,
        center.y - distance,
        center.z + distance * 0.55,
      );
      controls.update();
      controls.saveState();
      robotRef.current = robot;
      updatePose(robot);
    };

    const loader = new URDFLoader();
    const loadMesh = loader.defaultMeshLoader.bind(loader);
    loader.loadMeshCb = (url, manager, material, onLoad) => {
      pendingMeshLoads += 1;
      const finish = (mesh: THREE.Object3D, error?: Error) => {
        if (error) {
          loadErrors.push(url);
        } else {
          onLoad(mesh);
        }
        pendingMeshLoads -= 1;
        fitCameraToRobot();
      };
      try {
        loadMesh(url, manager, material, finish);
      } catch (error) {
        loadErrors.push(url);
        pendingMeshLoads -= 1;
        fitCameraToRobot();
        setLoadError(
          error instanceof Error ? error.message : "无法读取本地模型 mesh。",
        );
      }
    };
    loader.load(
      modelUrl,
      (loadedRobot) => {
        if (disposed) {
          disposeRobot(loadedRobot);
          return;
        }
        robot = loadedRobot;
        robotParsed = true;
        scene.add(loadedRobot);
        fitCameraToRobot();
      },
      undefined,
      (error: unknown) => {
        if (disposed) return;
        setStatus("load-error");
        setLoadError(
          error instanceof Error ? error.message : "无法读取本地 URDF。",
        );
      },
    );

    renderer.setAnimationLoop(() => {
      controls.update();
      renderer.render(scene, camera);
    });

    return () => {
      disposed = true;
      resizeObserver.disconnect();
      renderer.setAnimationLoop(null);
      controls.dispose();
      controlsRef.current = null;
      renderer.dispose();
      if (robot) {
        scene.remove(robot);
        disposeRobot(robot);
      }
      host.removeChild(renderer.domElement);
      scene.clear();
    };
  }, []);

  useEffect(() => {
    const robot = robotRef.current;
    if (!robot) return;
    const next = applyTelemetryPose(robot, joints, hipImuRpy);
    setApplication(next);
    setStatus(derivePoseStatus(next, lowStateState, hipImuState, modeMachine));
  }, [joints, hipImuRpy, lowStateState, hipImuState, modeMachine]);

  return (
    <section className="pose-panel" aria-label="G1 三维姿态模型">
      <div className="section-heading">
        <div>
          <p className="eyebrow">MEASURED POSE · UNITREE URDF</p>
          <h2>G1 实测姿态</h2>
        </div>
        <button
          className="view-reset"
          type="button"
          onClick={() => controlsRef.current?.reset()}
          disabled={status === "load-error"}
        >
          重置视角
        </button>
      </div>
      <div className="pose-toolbar">
        <span>拖动旋转 · 滚轮缩放</span>
        <span>
          LowState {lowStateState} · IMU {hipImuState} · 年龄：
          {lowStateAgeMs === null ? "—" : `${lowStateAgeMs} ms`} /
          {hipImuAgeMs === null ? "—" : ` ${hipImuAgeMs} ms`}
        </span>
      </div>
      <div className="pose-canvas" ref={hostRef} aria-label="G1 29DoF 三维模型" />
      <p
        className={`pose-status pose-status-${status}`}
        role={status === "load-error" ? "alert" : "status"}
      >
        {statusMessage(
          status,
          application,
          lowStateState,
          hipImuState,
          modeMachine,
        )}
        {loadError ? ` ${loadError}` : ""}
      </p>
      <p className="pose-disclaimer">
        本模型为 Unitree 官方 G1 描述模型，仅显示关节与 IMU 实测姿态；不进行动力学仿真、平衡证明或安全验收。
      </p>
    </section>
  );
}
