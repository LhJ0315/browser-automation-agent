"""Artifact stores: local disk (default) or S3/MinIO."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from settings import Settings, get_settings


@dataclass(frozen=True)
class ScreenshotMeta:
    name: str
    path: str  # relative path under the run, e.g. steps/01_home.png


class ArtifactStore(Protocol):
    def get_report(self, run_id: str) -> bytes | None: ...

    def list_screenshots(self, run_id: str) -> list[ScreenshotMeta]: ...

    def open_screenshot(self, run_id: str, name: str) -> tuple[bytes, str] | None:
        """Return (body, content_type) or None if missing."""
        ...

    def upload_run(self, run_id: str, local_run_dir: Path) -> str:
        """Sync a local run directory; return artifact URI (or local path URI)."""
        ...

    def run_exists(self, run_id: str) -> bool: ...


def _validate_run_id(run_id: str) -> str:
    if (
        not run_id
        or "/" in run_id
        or "\\" in run_id
        or ".." in run_id
        or run_id in {".", ".."}
    ):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return run_id


def _validate_screenshot_name(name: str) -> str:
    if "/" in name or "\\" in name or name in {".", ".."} or not name.endswith(".png"):
        raise ValueError(f"invalid screenshot name: {name!r}")
    return name


class LocalArtifactStore:
    def __init__(self, runs_root: Path) -> None:
        self.runs_root = Path(runs_root)

    def _run_dir(self, run_id: str) -> Path:
        run_id = _validate_run_id(run_id)
        run_dir = (self.runs_root / run_id).resolve()
        root = self.runs_root.resolve()
        if root not in run_dir.parents and run_dir != root:
            raise ValueError("invalid run path")
        return run_dir

    def run_exists(self, run_id: str) -> bool:
        try:
            return self._run_dir(run_id).is_dir()
        except ValueError:
            return False

    def get_report(self, run_id: str) -> bytes | None:
        path = self._run_dir(run_id) / "report.md"
        if not path.is_file():
            return None
        return path.read_bytes()

    def list_screenshots(self, run_id: str) -> list[ScreenshotMeta]:
        steps = self._run_dir(run_id) / "steps"
        if not steps.is_dir():
            return []
        out: list[ScreenshotMeta] = []
        for path in sorted(steps.glob("*.png")):
            out.append(
                ScreenshotMeta(name=path.name, path=f"steps/{path.name}")
            )
        return out

    def open_screenshot(self, run_id: str, name: str) -> tuple[bytes, str] | None:
        name = _validate_screenshot_name(name)
        run_dir = self._run_dir(run_id)
        path = (run_dir / "steps" / name).resolve()
        steps_dir = (run_dir / "steps").resolve()
        if steps_dir not in path.parents and path.parent != steps_dir:
            raise ValueError("path escape blocked")
        if not path.is_file():
            return None
        return path.read_bytes(), "image/png"

    def upload_run(self, run_id: str, local_run_dir: Path) -> str:
        # Already on disk; URI is a file:// hint for debugging.
        _validate_run_id(run_id)
        return local_run_dir.resolve().as_uri()


class S3ArtifactStore:
    """boto3 client pointed at MinIO / S3. Objects under runs/{run_id}/..."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        staging_root: Path | None = None,
    ) -> None:
        import boto3
        from botocore.client import Config

        self.bucket = bucket
        self.staging_root = Path(staging_root) if staging_root else None
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )
        self.ensure_bucket()

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except Exception:
                # Race / already exists / policy — head again
                self._client.head_bucket(Bucket=self.bucket)

    def _key(self, run_id: str, *parts: str) -> str:
        run_id = _validate_run_id(run_id)
        return "/".join(("runs", run_id, *parts))

    def run_exists(self, run_id: str) -> bool:
        prefix = self._key(run_id) + "/"
        resp = self._client.list_objects_v2(
            Bucket=self.bucket, Prefix=prefix, MaxKeys=1
        )
        return bool(resp.get("Contents"))

    def get_report(self, run_id: str) -> bytes | None:
        key = self._key(run_id, "report.md")
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            # botocore ClientError 404
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return obj["Body"].read()

    def list_screenshots(self, run_id: str) -> list[ScreenshotMeta]:
        prefix = self._key(run_id, "steps") + "/"
        out: list[ScreenshotMeta] = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for item in resp.get("Contents") or []:
                key = item["Key"]
                name = key.rsplit("/", 1)[-1]
                if name.endswith(".png"):
                    out.append(ScreenshotMeta(name=name, path=f"steps/{name}"))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        out.sort(key=lambda s: s.name)
        return out

    def open_screenshot(self, run_id: str, name: str) -> tuple[bytes, str] | None:
        name = _validate_screenshot_name(name)
        key = self._key(run_id, "steps", name)
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            # also catch NoSuchKey attribute if present
            if type(exc).__name__ == "NoSuchKey":
                return None
            raise
        ctype = obj.get("ContentType") or "image/png"
        return obj["Body"].read(), ctype

    def upload_run(self, run_id: str, local_run_dir: Path) -> str:
        run_id = _validate_run_id(run_id)
        local_run_dir = Path(local_run_dir)
        if not local_run_dir.is_dir():
            raise FileNotFoundError(f"local run dir missing: {local_run_dir}")
        for path in local_run_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(local_run_dir).as_posix()
            key = self._key(run_id, *rel.split("/"))
            ctype, _ = mimetypes.guess_type(path.name)
            extra = {"ContentType": ctype} if ctype else {}
            self._client.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs=extra or None,
            )
        return f"s3://{self.bucket}/runs/{run_id}"


def build_artifact_store(settings: Settings | None = None) -> ArtifactStore:
    cfg = settings or get_settings()
    backend = (cfg.artifact_store or "local").lower()
    if backend == "local":
        return LocalArtifactStore(cfg.runs_root)
    if backend == "s3":
        return S3ArtifactStore(
            endpoint=cfg.s3_endpoint,
            access_key=cfg.s3_access_key,
            secret_key=cfg.s3_secret_key,
            bucket=cfg.s3_bucket,
            region=cfg.s3_region,
            staging_root=cfg.runs_root,
        )
    raise ValueError(f"unknown ARTIFACT_STORE={backend!r} (expected local|s3)")


@lru_cache(maxsize=1)
def get_artifact_store() -> ArtifactStore:
    return build_artifact_store()


def reset_artifact_store_cache() -> None:
    get_artifact_store.cache_clear()
