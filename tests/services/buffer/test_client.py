from app.services.buffer.client import connect
from config.redis import RedisConfig


def test_connect_uses_url_and_decodes_text():
    client = connect(RedisConfig(url="redis://localhost:6379/0"))
    assert client.connection_pool.connection_kwargs["decode_responses"] is True
