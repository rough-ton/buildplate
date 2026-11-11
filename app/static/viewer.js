// 3D viewer: printer build plate sized from the printer profile, STL part, orbit controls.
import * as THREE from "three";
import { STLLoader } from "/vendor/three/STLLoader.js";
import { OrbitControls } from "/vendor/three/OrbitControls.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

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
    this.setupHover();
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
    if (this.plate) { this.scene.remove(this.plate); this.plate.traverse((o) => o.geometry && o.geometry.dispose()); }
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
    const axis = (to, color) => {
      const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0.02), to]);
      g.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color })));
    };
    axis(new THREE.Vector3(20, 0, 0.02), 0xd0453a);
    axis(new THREE.Vector3(0, 20, 0.02), 0x3a9d4f);
    this.plate = g;
    this.scene.add(g);
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
    this.unhover();
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
    this.unhover();
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

  // Hovering the part shows its size along X, Y and Z (the box it occupies on the
  // plate) next to the cursor, and outlines that box. Mouse and pen only: on touch
  // the size pill in the readout already shows it.
  setupHover() {
    this.raycaster = new THREE.Raycaster();
    this.tip = document.createElement("div");
    this.tip.className = "dims-tip tabular";
    this.tip.hidden = true;
    this.el.appendChild(this.tip);
    this.hoverBox = null;
    const cv = this.renderer.domElement;
    let pending = null;
    this._onMove = (e) => {
      if (e.pointerType === "touch" || e.buttons) { this.unhover(); return; } // not while orbiting
      if (!pending) requestAnimationFrame(() => { const ev = pending; pending = null; this.hover(ev); });
      pending = e;
    };
    this._onLeave = () => this.unhover();
    cv.addEventListener("pointermove", this._onMove);
    cv.addEventListener("pointerleave", this._onLeave);
    cv.addEventListener("pointerdown", this._onLeave);
  }

  hover(e) {
    if (!this.mesh) return this.unhover();
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const box = this.mesh.geometry.boundingBox;
    // Cheap box test first, so a big mesh is only ray-traced when the cursor is near it.
    if (!this.raycaster.ray.intersectsBox(box) || !this.raycaster.intersectObject(this.mesh, false).length) return this.unhover();
    const size = box.getSize(new THREE.Vector3());
    const mm = (n) => +n.toFixed(1);
    this.tip.textContent = `X ${mm(size.x)} · Y ${mm(size.y)} · Z ${mm(size.z)} mm`;
    this.tip.hidden = false;
    // Next to the cursor, flipped to stay inside the viewer.
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const w = this.tip.offsetWidth, h = this.tip.offsetHeight;
    this.tip.style.left = `${x + 14 + w > rect.width ? x - 10 - w : x + 14}px`;
    this.tip.style.top = `${y + 18 + h > rect.height ? y - 10 - h : y + 18}px`;
    if (!this.hoverBox) {
      this.hoverBox = new THREE.Box3Helper(box.clone(), new THREE.Color(css("--accent") || "#0E8C7A"));
      this.scene.add(this.hoverBox);
    }
  }

  unhover() {
    if (this.tip) this.tip.hidden = true;
    if (this.hoverBox) {
      this.scene.remove(this.hoverBox);
      this.hoverBox.geometry.dispose();
      this.hoverBox.material.dispose();
      this.hoverBox = null;
    }
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    const cv = this.renderer.domElement;
    cv.removeEventListener("pointermove", this._onMove);
    cv.removeEventListener("pointerleave", this._onLeave);
    cv.removeEventListener("pointerdown", this._onLeave);
    this.unhover();
    this.tip.remove();
    this._ro.disconnect();
    this._mq.removeEventListener("change", this._onScheme);
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
