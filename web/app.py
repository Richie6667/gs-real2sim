"""FastAPI service for queued, mobile-triggered 3D object generation."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import warnings
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.background import BackgroundTask


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_PROMPT_LENGTH = 80
LOG_TAIL_BYTES = 16 * 1024
ACTIVE_STATUSES = {"running", "stage1", "stage2", "stage3"}
TERMINAL_STATUSES = {"completed", "partial", "failed", "interrupted"}
PROMPT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _'-]*$")
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
ALLOWED_ARTIFACT_SUFFIXES = {
    ".glb",
    ".jpeg",
    ".jpg",
    ".json",
    ".log",
    ".mtl",
    ".npy",
    ".obj",
    ".ply",
    ".png",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_job_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


def _normalize_prompt(value: str) -> str:
    prompt = " ".join(value.strip().split())
    if not prompt:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_prompt", "message": "Object name is required."},
        )
    if len(prompt) > MAX_PROMPT_LENGTH or not PROMPT_RE.fullmatch(prompt):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_prompt",
                "message": (
                    "Use an English object name starting with a letter and containing "
                    "only letters, numbers, spaces, underscores, apostrophes, or hyphens."
                ),
            },
        )
    return prompt


def _normalize_image(payload: bytes) -> tuple[bytes, int, int]:
    if not payload:
        raise ValueError("empty_image")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("image_too_large")
                source.load()
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                width, height = normalized.size
                output = io.BytesIO()
                normalized.save(
                    output,
                    format="JPEG",
                    quality=95,
                    optimize=True,
                    progressive=True,
                )
                return output.getvalue(), width, height
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("invalid_image") from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("image_too_large") from exc


class JobStore:
    """Atomic filesystem-backed job state and artifact manifests."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.root / _safe_job_id(job_id)

    def state_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def create(self, prompt: str, image_bytes: bytes, width: int, height: int) -> dict[str, Any]:
        job_id = str(uuid4())
        created_at = utc_now()
        job_dir = self.root / job_id
        job_dir.mkdir(parents=False, exist_ok=False)
        source_path = job_dir / "source.jpg"
        source_path.write_bytes(image_bytes)
        state: dict[str, Any] = {
            "id": job_id,
            "prompt": prompt,
            "status": "queued",
            "progress": 0,
            "message": "Waiting for the GPU worker.",
            "error_code": None,
            "error_detail": None,
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "finished_at": None,
            "image": {"width": width, "height": height, "bytes": len(image_bytes)},
            "artifacts": [],
        }
        state["artifacts"] = self.scan_artifacts(job_id)
        self.write(state)
        return state

    def read(self, job_id: str) -> dict[str, Any]:
        state_path = self.state_path(job_id)
        try:
            with self._lock, state_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        if state.get("id") != _safe_job_id(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return state

    def write(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        state_path = self.state_path(state["id"])
        tmp_path = state_path.with_name(f".{state_path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(state, indent=2, ensure_ascii=True).encode("utf-8")
        with self._lock:
            try:
                with tmp_path.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, state_path)
            finally:
                tmp_path.unlink(missing_ok=True)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self.read(job_id)
            state.update(changes)
            self.write(state)
            return state

    def log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "pipeline.log"

    def log_tail(self, job_id: str) -> str:
        path = self.log_path(job_id)
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                return handle.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def append_log(self, job_id: str, text: str) -> None:
        with self._lock, self.log_path(job_id).open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()

    @staticmethod
    def _artifact_kind(relative_path: Path) -> str:
        name = relative_path.name.lower()
        parts = {part.lower() for part in relative_path.parts}
        suffix = relative_path.suffix.lower()
        if name in {"source.jpg", "input_image.png", "input_image.jpg"}:
            return "input"
        if name == "clean_background.png":
            return "background"
        if "mask" in name or (
            suffix in {".jpeg", ".jpg", ".png"}
            and ({"masks", "objects"} & parts)
        ):
            return "mask"
        if name.startswith("depth"):
            return "depth"
        if name.endswith("_keyframe.npy"):
            return "keyframe"
        if suffix in {".obj", ".glb"}:
            return "mesh"
        if suffix == ".ply":
            return "point_cloud"
        if name == "report.json":
            return "report"
        if suffix == ".json":
            return "metadata"
        if suffix == ".log":
            return "log"
        return "other"

    @staticmethod
    def _artifact_id(relative_path: Path) -> str:
        encoded = base64.urlsafe_b64encode(relative_path.as_posix().encode("utf-8"))
        return encoded.decode("ascii").rstrip("=")

    def scan_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        job_dir = self.job_dir(job_id)
        artifacts: list[dict[str, Any]] = []
        if not job_dir.is_dir():
            return artifacts
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(job_dir)
            name = relative.name
            if name == "job.json" or name.startswith(".job.json."):
                continue
            if name.startswith("download.") or path.suffix.lower() not in ALLOWED_ARTIFACT_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            artifacts.append(
                {
                    "id": self._artifact_id(relative),
                    "name": name,
                    "kind": self._artifact_kind(relative),
                    "relative_path": relative.as_posix(),
                    "size": size,
                }
            )
        return artifacts

    def refresh_artifacts(self, job_id: str) -> dict[str, Any]:
        return self.update(job_id, artifacts=self.scan_artifacts(job_id))

    def resolve_artifact(self, job_id: str, artifact_id: str) -> tuple[dict[str, Any], Path]:
        if not ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise HTTPException(status_code=404, detail="Artifact not found")
        state = self.read(job_id)
        artifact = next(
            (item for item in state.get("artifacts", []) if item.get("id") == artifact_id),
            None,
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")

        job_dir = self.job_dir(job_id).resolve()
        path = (job_dir / artifact["relative_path"]).resolve()
        try:
            path.relative_to(job_dir)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact, path

    def recover(self) -> list[str]:
        """Interrupt active jobs and return queued jobs in creation order."""
        self.ensure_root()
        queued: list[tuple[str, str]] = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            try:
                job_id = _safe_job_id(directory.name)
                state = self.read(job_id)
            except HTTPException:
                continue
            status = state.get("status")
            if status in ACTIVE_STATUSES:
                state.update(
                    status="interrupted",
                    progress=state.get("progress", 0),
                    message="Generation was interrupted by a service restart.",
                    error_code="service_restarted",
                    error_detail="The web worker stopped before the pipeline completed.",
                    finished_at=utc_now(),
                    artifacts=self.scan_artifacts(job_id),
                )
                self.write(state)
            elif status == "queued":
                queued.append((state.get("created_at", ""), job_id))
        queued.sort()
        return [job_id for _created_at, job_id in queued]


class PipelineService:
    """Owns the process-local, strictly serial GPU work queue."""

    def __init__(self, project_root: Path, store: JobStore):
        self.project_root = project_root
        self.store = store
        self.queue: asyncio.Queue[str] | None = None
        self.pending: list[str] = []
        self.worker_task: asyncio.Task[None] | None = None
        self.current_process: asyncio.subprocess.Process | None = None
        self.current_job_id: str | None = None

    async def start(self) -> None:
        self.queue = asyncio.Queue()
        recovered = self.store.recover()
        self.pending = list(recovered)
        for job_id in recovered:
            self.queue.put_nowait(job_id)
        self.worker_task = asyncio.create_task(self._worker(), name="sam3d-gpu-worker")

    async def stop(self) -> None:
        if self.worker_task is None:
            return
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        self.worker_task = None
        self.queue = None

    async def enqueue(self, job_id: str) -> None:
        if self.queue is None or self.worker_task is None or self.worker_task.done():
            raise HTTPException(status_code=503, detail="GPU worker is not available")
        self.pending.append(job_id)
        self.queue.put_nowait(job_id)

    def queue_position(self, job_id: str, status: str) -> int | None:
        if status in {"stage1", "stage2", "stage3"}:
            return 0
        if status != "queued":
            return None
        running_offset = 1 if self.current_job_id is not None else 0
        try:
            return self.pending.index(job_id) + 1 + running_offset
        except ValueError:
            return 1 + running_offset

    def worker_alive(self) -> bool:
        return self.worker_task is not None and not self.worker_task.done()

    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            job_id = await self.queue.get()
            try:
                if job_id in self.pending:
                    self.pending.remove(job_id)
                self.current_job_id = job_id
                await self._run_job(job_id)
            except asyncio.CancelledError:
                await self._interrupt_job(job_id)
                raise
            except Exception as exc:  # Keep the singleton worker alive for later jobs.
                with suppress(Exception):
                    self.store.append_log(
                        job_id, f"\n[web] Unhandled worker error: {exc!r}\n"
                    )
                    self._finish(
                        job_id,
                        status="failed",
                        message="The web worker failed unexpectedly.",
                        error_code="worker_error",
                        error_detail=str(exc),
                    )
            finally:
                self.current_job_id = None
                self.current_process = None
                self.queue.task_done()

    def _pythonpath(self) -> str:
        paths = [
            str(self.project_root),
            str(self.project_root / "submodule" / "Sam-3d-objects" / "notebook"),
            str(self.project_root / "submodule" / "Sam-3d-objects"),
        ]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        return os.pathsep.join(paths)

    def _commands(self, job_id: str, prompt: str) -> list[tuple[str, list[str]]]:
        job_dir = self.store.job_dir(job_id)
        python = sys.executable
        return [
            (
                "stage1",
                [
                    python,
                    str(self.project_root / "submodule" / "Prompt-Inpaint" / "main.py"),
                    "--resize-output",
                    "--save-individual-masks",
                    "--config",
                    str(
                        self.project_root
                        / "submodule"
                        / "Prompt-Inpaint"
                        / "configs"
                        / "items.yml"
                    ),
                    "--image",
                    str(job_dir / "source.jpg"),
                    "--output-dir",
                    str(job_dir),
                    "--prompts",
                    prompt,
                ],
            ),
            (
                "stage2",
                [
                    python,
                    str(self.project_root / "pipeline" / "background_reconstruction.py"),
                    str(job_dir),
                ],
            ),
            (
                "stage3",
                [
                    python,
                    str(self.project_root / "pipeline" / "objects_generation.py"),
                    "--project-root",
                    str(self.project_root / "submodule" / "Sam-3d-objects"),
                    "--input-dir",
                    str(job_dir),
                ],
            ),
        ]

    async def _run_job(self, job_id: str) -> None:
        state = self.store.read(job_id)
        if state.get("status") != "queued":
            return
        self.store.update(job_id, started_at=utc_now())

        stage_meta = {
            "stage1": (10, "Segmenting the object and reconstructing the background."),
            "stage2": (40, "Reconstructing the background and camera geometry."),
            "stage3": (70, "Generating the object mesh and point cloud."),
        }
        for stage, command in self._commands(job_id, state["prompt"]):
            progress, message = stage_meta[stage]
            self.store.update(
                job_id,
                status=stage,
                progress=progress,
                message=message,
                error_code=None,
                error_detail=None,
            )
            return_code = await self._run_command(job_id, stage, command)
            self.store.refresh_artifacts(job_id)
            if return_code != 0:
                status = "failed" if stage == "stage1" else "partial"
                self._finish(
                    job_id,
                    status=status,
                    message=f"{stage.title()} exited with code {return_code}.",
                    error_code=f"{stage}_process_failed",
                    error_detail=f"Process exit code: {return_code}",
                )
                return

            valid, code, detail = self._validate_stage(job_id, stage)
            if not valid:
                status = "failed" if stage == "stage1" else "partial"
                self._finish(
                    job_id,
                    status=status,
                    message=f"{stage.title()} did not produce all required outputs.",
                    error_code=code,
                    error_detail=detail,
                )
                return

        complete, detail = self._validate_object_sets(job_id)
        if complete:
            self._finish(
                job_id,
                status="completed",
                message="3D generation completed.",
                error_code=None,
                error_detail=None,
            )
        else:
            self._finish(
                job_id,
                status="partial",
                message="3D generation completed with missing object outputs.",
                error_code="stage3_incomplete",
                error_detail=detail,
            )

    async def _run_command(self, job_id: str, stage: str, command: list[str]) -> int:
        env = os.environ.copy()
        env["PYTHONPATH"] = self._pythonpath()
        env["PYTHONUNBUFFERED"] = "1"
        self.store.append_log(
            job_id,
            f"\n{'=' * 72}\n[{utc_now()}] {stage}: {shlex.join(command)}\n{'=' * 72}\n",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.project_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            self.store.append_log(job_id, f"[web] Failed to start process: {exc}\n")
            return 127

        self.current_process = process
        assert process.stdout is not None
        try:
            with self.store.log_path(job_id).open("ab") as log_handle:
                while True:
                    chunk = await process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    log_handle.write(chunk)
                    log_handle.flush()
            return await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        finally:
            self.current_process = None

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def _interrupt_job(self, job_id: str) -> None:
        if self.current_process is not None:
            await self._terminate_process(self.current_process)
        try:
            state = self.store.read(job_id)
        except HTTPException:
            return
        if state.get("status") in ACTIVE_STATUSES:
            self._finish(
                job_id,
                status="interrupted",
                message="Generation was interrupted while the service stopped.",
                error_code="service_stopped",
                error_detail="The active pipeline process was terminated.",
            )

    def _validate_stage(self, job_id: str, stage: str) -> tuple[bool, str, str]:
        job_dir = self.store.job_dir(job_id)
        if stage == "stage1":
            report_path = job_dir / "report.json"
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return False, "stage1_report_missing", "report.json is missing or invalid."
            if not isinstance(report, dict):
                return False, "stage1_report_missing", "report.json is not an object."
            try:
                num_objects = int(report.get("num_objects", 0))
            except (TypeError, ValueError):
                num_objects = 0
            if num_objects < 1:
                return False, "stage1_no_object", "The requested object was not detected."
            required = [
                job_dir / "input_image.png",
                job_dir / "clean_background.png",
            ]
            masks = list((job_dir / "masks").glob("*.png"))
            missing = [path.name for path in required if not path.is_file()]
            if missing or not masks:
                detail = f"Missing Stage 1 artifacts: {', '.join(missing) or 'masks/*.png'}"
                return False, "stage1_artifacts_missing", detail
            return True, "", ""

        if stage == "stage2":
            required = [
                job_dir / "extrinsic.npy",
                job_dir / "intrinsic.npy",
                job_dir / "depth.npy",
                job_dir / "depth_visual.png",
                job_dir / "depth_ori.npy",
                job_dir / "depth_ori_visual.png",
                job_dir / "3d_assets" / "bg.ply",
            ]
            missing = [path.relative_to(job_dir).as_posix() for path in required if not path.is_file()]
            if missing:
                return (
                    False,
                    "stage2_artifacts_missing",
                    f"Missing Stage 2 artifacts: {', '.join(missing)}",
                )
            return True, "", ""

        if stage == "stage3":
            complete, detail = self._validate_object_sets(job_id)
            if not complete and not list((job_dir / "3d_assets").glob("*.obj")):
                return False, "stage3_artifacts_missing", detail
            return True, "", ""

        return False, "invalid_stage", f"Unknown pipeline stage: {stage}"

    def _validate_object_sets(self, job_id: str) -> tuple[bool, str]:
        job_dir = self.store.job_dir(job_id)
        masks = {
            re.sub(r"[^0-9a-zA-Z_-]", "", path.stem)
            for path in (job_dir / "masks").glob("*.png")
        }
        assets_dir = job_dir / "3d_assets"
        obj_stems = {path.stem for path in assets_dir.glob("*.obj")}
        ply_stems = {path.stem for path in assets_dir.glob("*.ply")}
        keyframe_stems = {
            path.name[: -len("_keyframe.npy")]
            for path in assets_dir.glob("*_keyframe.npy")
        }
        complete = obj_stems & ply_stems & keyframe_stems
        if not complete:
            return False, "No matching OBJ, PLY, and keyframe files were generated."
        missing = sorted(masks - complete)
        if missing:
            return False, f"Incomplete object outputs for: {', '.join(missing)}"
        return True, ""

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        message: str,
        error_code: str | None,
        error_detail: str | None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal status: {status}")
        progress = 100 if status in {"completed", "partial"} else self.store.read(job_id).get("progress", 0)
        self.store.update(
            job_id,
            status=status,
            progress=progress,
            message=message,
            error_code=error_code,
            error_detail=error_detail,
            finished_at=utc_now(),
            artifacts=self.store.scan_artifacts(job_id),
        )


def _public_artifact(job_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": artifact["id"],
        "name": artifact["name"],
        "kind": artifact["kind"],
        "url": f"/api/jobs/{job_id}/artifacts/{artifact['id']}",
        "size": artifact["size"],
    }


def _public_job(service: PipelineService, state: dict[str, Any]) -> dict[str, Any]:
    job_id = state["id"]
    return {
        "id": job_id,
        "prompt": state.get("prompt"),
        "status": state.get("status"),
        "progress": state.get("progress", 0),
        "message": state.get("message", ""),
        "error_code": state.get("error_code"),
        "error_detail": state.get("error_detail"),
        "queue_position": service.queue_position(job_id, state.get("status", "")),
        "artifacts": [_public_artifact(job_id, item) for item in state.get("artifacts", [])],
        "log_tail": service.store.log_tail(job_id),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "download_url": f"/api/jobs/{job_id}/download",
    }


def _build_job_archive(store: JobStore, job_id: str, state: dict[str, Any]) -> Path:
    job_dir = store.job_dir(job_id).resolve()
    fd, archive_name = tempfile.mkstemp(
        prefix="download.", suffix=".tmp.zip", dir=job_dir
    )
    os.close(fd)
    archive_path = Path(archive_name)
    try:
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for item in state.get("artifacts", []):
                _artifact, path = store.resolve_artifact(job_id, item["id"])
                archive.write(path, arcname=path.relative_to(job_dir).as_posix())
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


async def _gpu_available() -> tuple[bool, str]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return False, "nvidia-smi is not installed"
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-L",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
    except asyncio.TimeoutError as exc:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        return False, str(exc)
    except OSError as exc:
        return False, str(exc)
    if process.returncode != 0:
        return False, stderr.decode("utf-8", errors="replace").strip()
    output = stdout.decode("utf-8", errors="replace").strip()
    return bool(output), output or "No NVIDIA GPU was reported"


def create_app() -> FastAPI:
    project_root = Path(
        os.environ.get("SAM3D_PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    ).expanduser().resolve()
    jobs_root = Path(
        os.environ.get("SAM3D_JOBS_ROOT", project_root / "data" / "jobs")
    ).expanduser().resolve()
    store = JobStore(jobs_root)
    service = PipelineService(project_root, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="SAM3D Mobile Capture",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.pipeline_service = service
    router = APIRouter()
    static_dir = Path(__file__).resolve().parent / "static"

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", media_type="text/html")

    @router.post("/api/jobs", status_code=202)
    async def create_job(
        image: UploadFile = File(...),
        prompt: str = Form(...),
    ) -> dict[str, Any]:
        normalized_prompt = _normalize_prompt(prompt)
        try:
            payload = await image.read(MAX_UPLOAD_BYTES + 1)
        finally:
            await image.close()
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"code": "image_too_large", "message": "Image exceeds 20 MB."},
            )
        try:
            normalized, width, height = await asyncio.to_thread(_normalize_image, payload)
        except ValueError as exc:
            code = str(exc)
            message = (
                "Image exceeds 40 megapixels."
                if code == "image_too_large"
                else "The uploaded file is not a valid image."
            )
            raise HTTPException(status_code=422, detail={"code": code, "message": message}) from exc

        state = store.create(normalized_prompt, normalized, width, height)
        try:
            await service.enqueue(state["id"])
        except HTTPException:
            store.update(
                state["id"],
                status="failed",
                message="GPU worker is not available.",
                error_code="worker_unavailable",
                finished_at=utc_now(),
            )
            raise
        return _public_job(service, store.read(state["id"]))

    @router.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        state = store.read(_safe_job_id(job_id))
        return _public_job(service, state)

    @router.get("/api/jobs/{job_id}/artifacts/{artifact_id}")
    async def get_artifact(job_id: str, artifact_id: str) -> FileResponse:
        canonical_id = _safe_job_id(job_id)
        artifact, path = store.resolve_artifact(canonical_id, artifact_id)
        media_type = mimetypes.guess_type(artifact["name"])[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @router.get("/api/jobs/{job_id}/download")
    async def download_job(job_id: str) -> FileResponse:
        canonical_id = _safe_job_id(job_id)
        state = store.read(canonical_id)
        archive_path = await asyncio.to_thread(
            _build_job_archive, store, canonical_id, state
        )
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"sam3d-{canonical_id}.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    @router.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        pipeline_service: PipelineService = request.app.state.pipeline_service
        gpu_ok, gpu_detail = await _gpu_available()
        required_paths = [
            project_root / "submodule" / "Prompt-Inpaint" / "main.py",
            project_root / "submodule" / "Prompt-Inpaint" / "configs" / "items.yml",
            project_root / "submodule" / "Prompt-Inpaint" / "checkpoints" / "sam3.pt",
            project_root / "pipeline" / "background_reconstruction.py",
            project_root / "pipeline" / "objects_generation.py",
            project_root / "submodule" / "Sam-3d-objects" / "checkpoints" / "hf" / "pipeline.yaml",
        ]
        missing = [path.relative_to(project_root).as_posix() for path in required_paths if not path.is_file()]
        worker_ok = pipeline_service.worker_alive()
        ready = worker_ok and gpu_ok and not missing
        payload = {
            "status": "ok" if ready else "unhealthy",
            "ready": ready,
            "checks": {
                "worker": {"ok": worker_ok},
                "gpu": {"ok": gpu_ok, "detail": gpu_detail},
                "required_files": {"ok": not missing, "missing": missing},
            },
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    app.include_router(router)
    app.mount(
        "/static",
        StaticFiles(directory=static_dir, check_dir=False),
        name="static",
    )
    return app


app = create_app()
