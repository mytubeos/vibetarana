"""Real Mongo concurrency gate in CI; opt in locally with VIBETARANA_MONGO_TEST_URI."""
import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pymongo import AsyncMongoClient
from bot.core import db, scheduler, service
from bot.core.lease import WorkerLease

pytestmark=pytest.mark.skipif(not os.getenv('VIBETARANA_MONGO_TEST_URI'), reason='Mongo integration URI not configured')


def test_real_mongo_lease_exclusion_expiry_and_job_cancel(monkeypatch):
    async def run():
        client=AsyncMongoClient(os.environ['VIBETARANA_MONGO_TEST_URI'],serverSelectionTimeoutMS=5000)
        database=client['vibetarana_ci_'+uuid.uuid4().hex]
        monkeypatch.setattr(db,'db',database)
        try:
            first,second=WorkerLease(),WorkerLease()
            await first.acquire()
            with pytest.raises(RuntimeError,match='Another worker'):await second.acquire()
            assert await database.worker_leases.count_documents({'owner':first.owner})==len(first.keys)
            # Expired worker records can be replaced; stale release cannot delete the new owner.
            await database.worker_leases.update_many({}, {'$set':{'expires':datetime.now(timezone.utc)-timedelta(seconds=1)}})
            await second.acquire()
            await first.release()
            assert await database.worker_leases.count_documents({'owner':second.owner})==len(second.keys)
            await second.release()
            when=datetime.now(timezone.utc)+timedelta(minutes=1)
            job=await scheduler.create(-1,2,[],when,'UTC')
            assert not await scheduler.cancel(-2,job)
            assert await scheduler.cancel(-1,job)
            assert (await database.scheduled_sessions.find_one({'_id':job}))['status']=='cancelled'
        finally:
            await client.drop_database(database.name)
            await client.close()
    asyncio.run(run())
