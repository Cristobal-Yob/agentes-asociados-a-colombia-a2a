"""Small transactional document store. External effects never run in transactions."""

import asyncio
import hashlib
import json

from google.cloud.firestore_v1 import AsyncClient, async_transactional


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class FirestoreStore:
    def __init__(self, project, database):
        self.client = AsyncClient(project=project, database=database)

    async def atomic(self, key, mutate):
        reference = self.client.collection("colombia_ui_v1").document(digest(key))

        @async_transactional
        async def apply(transaction):
            snapshot = await reference.get(transaction=transaction, timeout=4, retry=None)
            row = snapshot.to_dict() if snapshot.exists else {}
            result = mutate(row)
            if len(json.dumps(row, ensure_ascii=False, default=str).encode()) > 700_000:
                raise ValueError("Workspace storage limit exceeded")
            if row:
                transaction.set(reference, row)
            else:
                transaction.delete(reference)
            return result

        async with asyncio.timeout(12):
            return await apply(self.client.transaction(max_attempts=3))

    async def close(self):
        self.client.close()
