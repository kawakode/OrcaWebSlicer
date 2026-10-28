/**
 * A Z-up three.js viewport shared by the plater and the preview. It renders on
 * demand rather than every frame, so an idle tab costs nothing.
 */

import { AmbientLight, Color, DirectionalLight, PerspectiveCamera, Scene, Vector3, WebGLRenderer } from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

export interface Viewport {
  scene: Scene;
  camera: PerspectiveCamera;
  renderer: WebGLRenderer;
  controls: OrbitControls;
  render: () => void;
  frame: (center: Vector3, size: number) => void;
  dispose: () => void;
}

export function createViewport(host: HTMLElement): Viewport {
  const scene = new Scene();
  scene.background = new Color(getComputedStyle(host).getPropertyValue("--viewport").trim() || "#1d232b");
  const camera = new PerspectiveCamera(40, 1, 0.5, 10000);
  camera.up.set(0, 0, 1);
  const renderer = new WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  host.appendChild(renderer.domElement);
  scene.add(new AmbientLight(0xffffff, 1.2));
  const sun = new DirectionalLight(0xffffff, 1.8);
  sun.position.set(-0.6, -1, 1.4);
  scene.add(sun);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;

  let pending = false;
  const render = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      renderer.render(scene, camera);
    });
  };
  controls.addEventListener("change", render);

  const resize = () => {
    const { clientWidth, clientHeight } = host;
    if (clientWidth === 0 || clientHeight === 0) return;
    renderer.setSize(clientWidth, clientHeight);
    camera.aspect = clientWidth / clientHeight;
    camera.updateProjectionMatrix();
    render();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(host);
  resize();

  const frame = (center: Vector3, size: number) => {
    controls.target.copy(center);
    camera.position.copy(center).add(new Vector3(0, -1.3, 1).multiplyScalar(size));
    camera.lookAt(center);
    controls.update();
    render();
  };

  return {
    scene,
    camera,
    renderer,
    controls,
    render,
    frame,
    dispose: () => {
      observer.disconnect();
      controls.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
