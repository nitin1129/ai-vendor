"""File storage in MongoDB GridFS (product photos and vendor documents)."""
import io
from dataclasses import dataclass, field
from typing import Any, Protocol

from bson import ObjectId
from bson.errors import InvalidId
from PIL import Image, ImageOps, UnidentifiedImageError

from app.utils import new_id

IMAGE_SIZES = {"thumb": 200, "medium": 600, "large": 1200}


@dataclass
class StoredFile:
    id: str
    filename: str
    content_type: str
    data: bytes
    metadata: dict = field(default_factory=dict)


class FileStore(Protocol):
    async def put(self, data: bytes, filename: str, content_type: str, metadata: dict) -> str: ...
    async def get(self, file_id: str) -> StoredFile | None: ...
    async def delete(self, file_id: str) -> None: ...


class GridFSStore:
    def __init__(self, db: Any, bucket: str = "files"):
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket

        self.bucket = AsyncIOMotorGridFSBucket(db, bucket_name=bucket)

    async def put(self, data: bytes, filename: str, content_type: str, metadata: dict) -> str:
        oid = await self.bucket.upload_from_stream(
            filename, data, metadata={**metadata, "content_type": content_type}
        )
        return str(oid)

    async def get(self, file_id: str) -> StoredFile | None:
        try:
            stream = await self.bucket.open_download_stream(ObjectId(file_id))
        except (InvalidId, Exception):
            return None
        data = await stream.read()
        meta = dict(stream.metadata or {})
        return StoredFile(file_id, stream.filename, meta.pop("content_type", "application/octet-stream"), data, meta)

    async def delete(self, file_id: str) -> None:
        try:
            await self.bucket.delete(ObjectId(file_id))
        except Exception:
            pass


class MemoryStore:
    def __init__(self) -> None:
        self.files: dict[str, StoredFile] = {}

    async def put(self, data: bytes, filename: str, content_type: str, metadata: dict) -> str:
        fid = new_id("file")
        self.files[fid] = StoredFile(fid, filename, content_type, data, dict(metadata))
        return fid

    async def get(self, file_id: str) -> StoredFile | None:
        return self.files.get(file_id)

    async def delete(self, file_id: str) -> None:
        self.files.pop(file_id, None)


_store: FileStore | None = None


def set_store(store: FileStore) -> None:
    global _store
    _store = store


def get_store() -> FileStore:
    if _store is None:
        raise RuntimeError("File store not configured")
    return _store


class BadImage(ValueError):
    pass


def make_image_variants(data: bytes) -> dict[str, bytes]:
    """Resize to fixed widths and re-encode as WebP (also strips EXIF/GPS metadata)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise BadImage("Not a valid image") from e
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    variants = {}
    for name, size in IMAGE_SIZES.items():
        copy = img.copy()
        copy.thumbnail((size, size))
        buf = io.BytesIO()
        copy.save(buf, "WEBP", quality=82, method=4)
        variants[name] = buf.getvalue()
    return variants
