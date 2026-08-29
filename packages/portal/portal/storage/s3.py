from __future__ import annotations

from hashlib import sha256

import aioboto3

from botocore.exceptions import ClientError

from portal.storage.port import ObjectReference


class S3ObjectStorage:
    """Immutable object storage against an S3-compatible endpoint (MinIO).

    Keys are `container/object_key`, matching what ObjectReference already
    tracks -- FileObjectStorage never used those fields since a single local
    directory needed no further namespacing.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        self._bucket = bucket
        self._session = aioboto3.Session()
        self._client_kwargs = {
            "endpoint_url": endpoint_url,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
        }

    async def put_immutable(
        self,
        reference: ObjectReference,
        content: bytes,
    ) -> None:
        if sha256(content).hexdigest() != reference.sha256:
            msg = "el contenido no coincide con la referencia inmutable"
            raise ValueError(msg)

        key = self._key(reference)

        async with self._session.client("s3", **self._client_kwargs) as client:
            try:
                existing = await client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                    raise
            else:
                body = await existing["Body"].read()
                if body != content:
                    msg = "la referencia de objeto inmutable ya existe"
                    raise ValueError(msg)
                return

            await client.put_object(Bucket=self._bucket, Key=key, Body=content)

    async def open(self, reference: ObjectReference) -> bytes:
        async with self._session.client("s3", **self._client_kwargs) as client:
            response = await client.get_object(
                Bucket=self._bucket, Key=self._key(reference)
            )
            return bytes(await response["Body"].read())

    @staticmethod
    def _key(reference: ObjectReference) -> str:
        return f"{reference.container}/{reference.object_key}"
