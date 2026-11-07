import asyncio
import hashlib
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Mapping,
    Protocol,
    Sequence,
    cast,
)

import aioboto3
import filetype
from aiobotocore.config import AioConfig
from botocore import UNSIGNED
from pydantic import BaseModel, ConfigDict
from pydantic_ai import BinaryImage

from lib.utils.common import PROJECT_ROOT
from lib.utils.image import resolve_local_image_paths

__all__ = [
    "ImageRef",
    "ImageSource",
    "LocalImageSource",
    "S3ImageSource",
    "S3ImageTemplate",
    "build_image_source",
    "parse_s3_image_uri",
]

OUTPUT_ROOT = PROJECT_ROOT / "output"

CASE_ID_PLACEHOLDER = "{case_id}"
INDEX_PLACEHOLDER = "{index}"

MAX_SLUG_LENGTH = 80


def _slugify(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-")
    if len(slug) > MAX_SLUG_LENGTH:
        digest = hashlib.blake2b(value.encode(), digest_size=4).hexdigest()
        slug = f"{slug[: MAX_SLUG_LENGTH - len(digest) - 1].rstrip('-')}-{digest}"
    return slug


def _binary_image_from_bytes(
    data: bytes, *, filename: str | None = None
) -> BinaryImage:
    media_type = filetype.guess_mime(data)
    if media_type is None and filename is not None:
        media_type, _ = mimetypes.guess_type(filename)
    return BinaryImage(data=data, media_type=media_type or "image/png")


def _unsigned(no_sign_request: bool) -> bool:
    if no_sign_request:
        return True
    return os.environ.get("AWS_NO_SIGN_REQUEST", "").lower() == "true"


class ImageRef(BaseModel):
    """Where an image lives, plus the md5 of its content."""

    model_config = ConfigDict(frozen=True)

    location: str
    """A path relative to `output/` for local images, an `s3://` url for S3 ones."""
    md5: str
    """From the S3 `ETag`, or computed while reading a local file."""


class ImageSource(Protocol):
    """Two-phase image access: resolve locations first, fetch bytes later.

    `resolve` is called once per case outside the graph so the run id can be derived from
    the image content, while `load` runs inside the graph node. Only `ImageRef`s are ever
    persisted; the bytes stay node-local.
    """

    @property
    def slug(self) -> str | None:
        """Filename suffix identifying the source, or `None` for the default local one."""

    async def resolve(self, case_id: str) -> list[ImageRef]: ...

    async def load(self, refs: Sequence[ImageRef]) -> list[BinaryImage]: ...


@dataclass(frozen=True)
class LocalImageSource:
    """Images under `output/<task_name>/images`, named `<case_id>_<index>.png`."""

    task_name: str

    @property
    def slug(self) -> None:
        return None

    @property
    def images_dir(self) -> Path:
        return OUTPUT_ROOT / self.task_name / "images"

    async def resolve(self, case_id: str) -> list[ImageRef]:
        return await asyncio.to_thread(self._resolve_sync, case_id)

    def _resolve_sync(self, case_id: str) -> list[ImageRef]:
        if not self.images_dir.is_dir():
            return []
        # There is no cheap equivalent of the S3 ETag here, so the files are read once to
        # fingerprint them and again when the node needs the bytes.
        return [
            ImageRef(
                location=path.relative_to(OUTPUT_ROOT).as_posix(),
                md5=hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest(),
            )
            for path in resolve_local_image_paths(self.images_dir, case_id)
        ]

    async def load(self, refs: Sequence[ImageRef]) -> list[BinaryImage]:
        return await asyncio.to_thread(self._load_sync, refs)

    def _load_sync(self, refs: Sequence[ImageRef]) -> list[BinaryImage]:
        return [
            _binary_image_from_bytes(
                (OUTPUT_ROOT / ref.location).read_bytes(),
                filename=PurePosixPath(ref.location).name,
            )
            for ref in refs
        ]


@dataclass(frozen=True)
class S3ImageTemplate:
    """`s3://<bucket>/<key-prefix>/{case_id}_{index}.<ext>` split apart.

    The endpoint is not part of the URI. It comes from `AWS_ENDPOINT_URL`,
    `AWS_ENDPOINT_URL_S3`, or `S3ImageSource.endpoint_url`.
    """

    bucket: str
    key_prefix: str
    """Everything in the key before `{case_id}`."""
    key_infix: str
    """Between `{case_id}` and `{index}`."""
    key_suffix: str
    """After `{index}`, including the extension."""
    extension: str

    @property
    def slug(self) -> str:
        return _slugify(f"{self.bucket}/{self.key_prefix}/{self.extension}")

    def list_prefix(self, case_id: str) -> str:
        return f"{self.key_prefix}{case_id}{self.key_infix}"

    def key_pattern(self, case_id: str) -> re.Pattern[str]:
        return re.compile(
            rf"^{re.escape(self.list_prefix(case_id))}(\d+){re.escape(self.key_suffix)}$"
        )

    def url(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def split_url(self, url: str) -> tuple[str, str]:
        if not url.startswith("s3://"):
            raise ValueError(f"{url!r} is not a valid s3 image url")
        bucket, _, key = url.removeprefix("s3://").partition("/")
        if not bucket or not key:
            raise ValueError(f"{url!r} is not a valid s3 image url")
        return bucket, key


def parse_s3_image_uri(uri: str) -> S3ImageTemplate:
    if not uri.startswith("s3://"):
        raise ValueError(
            f"{uri!r} must look like "
            f"s3://<bucket>/<key-prefix>/{CASE_ID_PLACEHOLDER}_{INDEX_PLACEHOLDER}.png. "
            "Put the endpoint in AWS_ENDPOINT_URL or pass --s3-endpoint-url."
        )

    bucket, _, key_template = uri.removeprefix("s3://").partition("/")
    if not bucket or not key_template:
        raise ValueError(
            f"{uri!r} must look like "
            f"s3://<bucket>/<key-prefix>/{CASE_ID_PLACEHOLDER}_{INDEX_PLACEHOLDER}.png"
        )

    for placeholder in (CASE_ID_PLACEHOLDER, INDEX_PLACEHOLDER):
        if key_template.count(placeholder) != 1:
            raise ValueError(
                f"The key template of {uri!r} must contain {placeholder} exactly once"
            )

    key_prefix, _, after_case_id = key_template.partition(CASE_ID_PLACEHOLDER)
    if INDEX_PLACEHOLDER not in after_case_id:
        raise ValueError(
            f"{INDEX_PLACEHOLDER} must come after {CASE_ID_PLACEHOLDER} in {uri!r}"
        )
    key_infix, _, key_suffix = after_case_id.partition(INDEX_PLACEHOLDER)

    _, dot, extension = key_suffix.rpartition(".")
    if not dot or not extension.isalnum():
        raise ValueError(f"The key template of {uri!r} must end with a file extension")

    return S3ImageTemplate(
        bucket=bucket,
        key_prefix=key_prefix,
        key_infix=key_infix,
        key_suffix=key_suffix,
        extension=extension,
    )


def _etag_md5(obj: Mapping[str, Any]) -> str | None:
    etag = obj.get("ETag")
    return etag.strip('"') if etag else None


S3_SESSION = aioboto3.Session()


def _client_config(no_sign_request: bool) -> AioConfig | None:
    """Leave addressing style and signing to boto unless anonymous reads are requested.

    boto3 reads `s3.addressing_style` from `~/.aws/config`. A custom endpoint already
    defaults to path-style unless that setting is `virtual`. `signature_version` is not
    one of those config keys, and boto3 does not read `AWS_NO_SIGN_REQUEST`, so unsigned
    requests are enabled only here.
    """
    if not no_sign_request:
        return None
    return AioConfig(signature_version=UNSIGNED)


@dataclass
class S3ImageSource:
    """Images in an S3-compatible store, discovered through prefix listing.

    Nothing is cached: one instance is shared by every case of a run, so held-onto bytes
    would pile up until the process is killed. Each `load` hands its images to the caller
    and keeps no reference to them.
    """

    template: S3ImageTemplate
    endpoint_url: str | None = None
    """Overrides `AWS_ENDPOINT_URL` / `AWS_ENDPOINT_URL_S3` when set.

    Left unset, the client is created without `endpoint_url` so boto keeps reading those
    variables. Passing `None` explicitly would clear them.
    """
    no_sign_request: bool = False
    concurrency: int = 16

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.concurrency)
        if self.endpoint_url == "":
            self.endpoint_url = None

    @property
    def slug(self) -> str:
        return self.template.slug

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        kwargs: dict[str, Any] = {}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if config := _client_config(self.no_sign_request):
            kwargs["config"] = config
        async with cast(
            AsyncContextManager[Any],
            S3_SESSION.client("s3", **kwargs),
        ) as s3:
            yield s3

    async def resolve(self, case_id: str) -> list[ImageRef]:
        pattern = self.template.key_pattern(case_id)

        matched: list[tuple[int, str, str | None]] = []
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self.template.bucket,
                Prefix=self.template.list_prefix(case_id),
            ):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if m := pattern.match(key):
                        matched.append((int(m.group(1)), key, _etag_md5(obj)))

            matched.sort(key=lambda t: t[0])
            md5s = await asyncio.gather(
                *(self._md5(s3, key, listed) for _, key, listed in matched)
            )

        return [
            ImageRef(location=self.template.url(key), md5=md5)
            for (_, key, _), md5 in zip(matched, md5s)
        ]

    async def _md5(self, s3: Any, key: str, listed_md5: str | None) -> str:
        if listed_md5 is not None:
            return listed_md5
        # Some S3 implementations leave the ETag out of listings; HEAD still carries it.
        async with self._semaphore:
            response = await s3.head_object(Bucket=self.template.bucket, Key=key)
        md5 = _etag_md5(response)
        if md5 is None:
            raise ValueError(
                f"{self.template.url(key)} has no ETag to fingerprint it with"
            )
        return md5

    async def load(self, refs: Sequence[ImageRef]) -> list[BinaryImage]:
        if not refs:
            return []

        async with self._client() as s3:
            return list(await asyncio.gather(*(self._fetch(s3, ref) for ref in refs)))

    async def _fetch(self, s3: Any, ref: ImageRef) -> BinaryImage:
        bucket, key = self.template.split_url(ref.location)
        async with self._semaphore:
            response = await s3.get_object(Bucket=bucket, Key=key)
            data = await response["Body"].read()
        return _binary_image_from_bytes(data, filename=PurePosixPath(key).name)


def build_image_source(
    task_name: str,
    image_s3_uri: str | None,
    s3_endpoint_url: str | None = None,
    no_sign_request: bool = False,
) -> ImageSource:
    if image_s3_uri is None:
        return LocalImageSource(task_name=task_name)
    return S3ImageSource(
        template=parse_s3_image_uri(image_s3_uri),
        endpoint_url=s3_endpoint_url or None,
        no_sign_request=_unsigned(no_sign_request),
    )
