import fakeredis
import pytest
from redis.exceptions import OutOfMemoryError

OOM_MESSAGE = "command not allowed when used memory > 'maxmemory'"


class _FullPipeline:
    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def execute(self, *args, **kwargs):
        raise OutOfMemoryError(OOM_MESSAGE)


class FullFakeRedis(fakeredis.FakeRedis):
    def sadd(self, *args, **kwargs):
        raise OutOfMemoryError(OOM_MESSAGE)

    def evalsha(self, *args, **kwargs):
        raise OutOfMemoryError(OOM_MESSAGE)

    def pipeline(self, *args, **kwargs):
        return _FullPipeline()


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def fake_redis_full():
    return FullFakeRedis(decode_responses=True)
