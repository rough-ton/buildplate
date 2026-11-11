// 3D viewer: printer build plate sized from the printer profile, STL part, orbit controls.
import * as THREE from "three";
import { STLLoader } from "/vendor/three/STLLoader.js";
import { OrbitControls } from "/vendor/three/OrbitControls.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// A letter that always faces the camera and keeps the same size on screen at any
// zoom (size is a fraction of the viewer's height).
function axisLabel(text, color, size = 0.032) {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const ctx = c.getContext("2d");
  ctx.font = "600 44px system-ui, -apple-system, 'Segoe UI', sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = color;
  ctx.fillText(text, 32, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, sizeAttenuation: false }));
  sprite.scale.set(size, size, 1);
  return sprite;
}

// Free a group's GPU resources: geometries, materials and their textures.
function disposeTree(root) {
  root.traverse((o) => {
    o.geometry?.dispose();
    for (const m of [].concat(o.material || [])) { m.map?.dispose(); m.dispose(); }
  });
}

export class Viewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.5, 20000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1, -1.4, 2);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0xffffff, 0.5);
    rim.position.set(-1.5, 1.2, 0.6);
    this.scene.add(rim);
    this.bed = { x: 256, y: 256, shape: "rect" };
    this.plate = null;
    this.mesh = null;
    this.edges = null;
    this.wire = false;
    this.loader = new STLLoader();
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(el);
    this._mq = window.matchMedia("(prefers-color-scheme: dark)");
    this._onScheme = () => { this.buildPlate(); this.recolor(); };
    this._mq.addEventListener("change", this._onScheme);
    this.buildPlate();
    this.resize();
    this.home("iso");
    const tick = () => {
      this._raf = requestAnimationFrame(tick);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    tick();
  }

  setBed(x, y, shape = "rect") {
    if (x === this.bed.x && y === this.bed.y && shape === this.bed.shape) return;
    this.bed = { x, y: shape === "round" ? x : y, shape };
    this.buildPlate();
  }

  buildPlate() {
    if (this.plate) { this.scene.remove(this.plate); disposeTree(this.plate); }
    const g = new THREE.Group();
    const { x, y } = this.bed;
    const major = new THREE.Color(css("--grid-major") || "#B7C1CB");
    const minor = new THREE.Color(css("--grid-minor") || "#CDD5DD");
    const round = this.bed.shape === "round";
    const r = x / 2;
    const surface = new THREE.Mesh(
      round ? new THREE.CircleGeometry(r, 96) : new THREE.PlaneGeometry(x, y),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(css("--viewer-bg") || "#E4E9EE").offsetHSL(0, 0, 0.025), transparent: true, opacity: 0.9 })
    );
    surface.position.z = -0.05;
    g.add(surface);
    const pts = { major: [], minor: [] };
    // Grid lines every 10 mm, bold every 50 mm, clipped to a circle on round beds.
    const span = (c, half) => (round ? Math.sqrt(Math.max(0, r * r - c * c)) : half);
    for (let i = -Math.floor(x / 2 / 10) * 10; i <= x / 2; i += 10) {
      const e = span(i, y / 2);
      if (e > 0) (i % 50 === 0 ? pts.major : pts.minor).push(i, -e, 0, i, e, 0);
    }
    for (let j = -Math.floor(y / 2 / 10) * 10; j <= y / 2; j += 10) {
      const e = span(j, x / 2);
      if (e > 0) (j % 50 === 0 ? pts.major : pts.minor).push(-e, j, 0, e, j, 0);
    }
    for (const [k, color] of [["minor", minor], ["major", major]]) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(pts[k], 3));
      g.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color })));
    }
    const outline = round
      ? Array.from({ length: 129 }, (_, i) => new THREE.Vector3(r * Math.cos((i / 128) * Math.PI * 2), r * Math.sin((i / 128) * Math.PI * 2), 0))
      : [new THREE.Vector3(-x / 2, -y / 2, 0), new THREE.Vector3(x / 2, -y / 2, 0), new THREE.Vector3(x / 2, y / 2, 0), new THREE.Vector3(-x / 2, y / 2, 0), new THREE.Vector3(-x / 2, -y / 2, 0)];
    g.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(outline), new THREE.LineBasicMaterial({ color: major })));
    g.add(this.buildAxes());
    this.plate = g;
    this.scene.add(g);
  }

  // X (red), Y (green) and Z (blue) arrows from the plate origin (its centre, where
  // parts are placed). X and Y run out to the plate edge, so they still show past a
  // part sitting on the origin and stay readable when the camera is close to it.
  // They are solid shapes, not 1 px lines, so they stand out from the grid.
  buildAxes() {
    const g = new THREE.Group();
    const { x, y } = this.bed;
    const size = Math.min(x, y);
    const shaft = Math.min(0.5, Math.max(0.25, size * 0.0014));
    const headLen = Math.min(7, Math.max(3.5, size * 0.022));
    const headR = headLen * 0.32;
    const zLen = Math.min(60, Math.max(25, size * 0.18));
    const colors = {
      x: css("--axis-x") || "#D0453A",
      y: css("--axis-y") || "#2E9A4A",
      z: css("--axis-z") || "#2F6BE0",
    };
    // X and Y sit on the plate (lifted by their radius) so they never z-fight the grid.
    const origin = new THREE.Vector3(0, 0, shaft);
    const up = new THREE.Vector3(0, 1, 0); // three.js cylinders and cones point along +Y
    const axes = [
      ["x", new THREE.Vector3(1, 0, 0), x / 2],
      ["y", new THREE.Vector3(0, 1, 0), y / 2],
      ["z", new THREE.Vector3(0, 0, 1), zLen],
    ];
    for (const [name, dir, len] of axes) {
      const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(colors[name]) });
      const turn = new THREE.Quaternion().setFromUnitVectors(up, dir);
      const body = new THREE.Mesh(new THREE.CylinderGeometry(shaft, shaft, len - headLen, 12), mat);
      body.quaternion.copy(turn);
      body.position.copy(origin).addScaledVector(dir, (len - headLen) / 2);
      const head = new THREE.Mesh(new THREE.ConeGeometry(headR, headLen, 20), mat);
      head.quaternion.copy(turn);
      head.position.copy(origin).addScaledVector(dir, len - headLen / 2);
      const label = axisLabel(name.toUpperCase(), colors[name]);
      label.position.copy(origin).addScaledVector(dir, len + headLen * 0.8);
      g.add(body, head, label);
    }
    return g;
  }

  recolor() {
    if (this.mesh) this.mesh.material.color = new THREE.Color(css("--part") || "#3C6E9E");
  }

  async load(url) {
    const geo = await this.loader.loadAsync(url);
    geo.computeVertexNormals();
    geo.computeBoundingBox();
    // Center the footprint on the bed and rest the part on the plate, like a slicer.
    // Only the preview moves; downloads keep the design's own coordinates.
    const bb = geo.boundingBox;
    const c = bb.getCenter(new THREE.Vector3());
    geo.translate(-c.x, -c.y, -bb.min.z);
    geo.computeBoundingBox();
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.geometry.dispose(); }
    if (this.edges) { this.scene.remove(this.edges); this.edges.geometry.dispose(); this.edges = null; }
    const mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(css("--part") || "#3C6E9E"), roughness: 0.55, metalness: 0.05, flatShading: false, wireframe: this.wire });
    this.mesh = new THREE.Mesh(geo, mat);
    this.scene.add(this.mesh);
    const first = !this._hasLoaded;
    this._hasLoaded = true;
    if (first) {
      if (this.el.clientWidth > 0) this.home(this._view || "iso");
      else this._needsHome = true;
    }
    return geo.boundingBox;
  }

  clear() {
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.geometry.dispose(); this.mesh = null; }
    this._hasLoaded = false;
  }

  toggleWire() {
    this.wire = !this.wire;
    if (this.mesh) this.mesh.material.wireframe = this.wire;
    return this.wire;
  }

  home(view = "iso") {
    this._view = view;
    let center = new THREE.Vector3(0, 0, 0);
    let radius = Math.max(this.bed.x, this.bed.y) * 0.35;
    if (this.mesh) {
      const box = this.mesh.geometry.boundingBox;
      center = box.getCenter(new THREE.Vector3());
      radius = Math.max(box.getSize(new THREE.Vector3()).length() * 0.5, 5);
    }
    const vfov = (this.camera.fov * Math.PI) / 180;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    const dist = (radius / Math.sin(Math.min(vfov, hfov) / 2)) * 1.12;
    const dirs = {
      iso: new THREE.Vector3(0.62, -0.95, 0.72),
      front: new THREE.Vector3(0, -1, 0.0001),
      right: new THREE.Vector3(1, 0, 0.0001),
      top: new THREE.Vector3(0, -0.0001, 1),
    };
    const d = (dirs[view] || dirs.iso).clone().normalize();
    this.camera.position.copy(center).add(d.multiplyScalar(dist));
    this.controls.target.copy(center);
    this.camera.near = dist / 100;
    this.camera.far = dist * 100;
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  resize() {
    const w = this.el.clientWidth || 1;
    const h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (this._needsHome && this.el.clientWidth > 0) {
      this._needsHome = false;
      this.home(this._view || "iso");
    }
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    this._mq.removeEventListener("change", this._onScheme);
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
