# Third-party software notices

HereTune is distributed under the license in [`LICENSE`](LICENSE). Its runtime dependencies are separate third-party projects and remain governed by their own licenses.

HereTune does **not** vendor those Python packages into this source repository. The installer/pip resolves them separately. This file is a convenient map, not a substitute for the license metadata and notices shipped by each dependency.

| Dependency | Purpose in HereTune | License family |
|---|---|---|
| FastAPI | HTTP/API application framework | MIT |
| Uvicorn | ASGI server | BSD-3-Clause |
| NumPy | Numerical image/statistical analysis | BSD-3-Clause (with bundled components carrying their own compatible notices) |
| OpenCV / `opencv-python-headless` | Image analysis and preview encoding | Apache-2.0 |
| psutil | Process/resource telemetry and cleanup | BSD-3-Clause |

Transitive dependencies such as Starlette, Pydantic, AnyIO, and others are installed by the Python package resolver and retain their own terms.

FFmpeg/FFprobe are external executables discovered on the user's machine; HereTune does not redistribute FFmpeg in this repository. Users should obtain an FFmpeg build whose licensing terms are appropriate for their environment.

HereSphere itself is **not** bundled with HereTune. No HereSphere executable, PAK, saved library, extracted game asset, encryption material, or proprietary runtime payload is redistributed by this repository.
