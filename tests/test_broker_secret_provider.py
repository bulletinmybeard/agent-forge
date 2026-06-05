import asyncio

from web.server.secret import BrokerSecretProvider, SecretBroker


def test_provider_prompts_then_caches():
    async def run():
        broker = SecretBroker()
        sent = []
        broker.set_sender(lambda m: sent.append(m))
        loop = asyncio.get_event_loop()
        provider = BrokerSecretProvider(broker, loop, ttl_seconds=300)

        async def answer_once():
            await asyncio.sleep(0.01)
            broker.resolve(sent[0]["request_id"], value="pw")

        get1 = asyncio.ensure_future(asyncio.to_thread(provider.get, "localhost"))
        await answer_once()
        assert await get1 == "pw"

        get2 = await asyncio.to_thread(provider.get, "localhost")
        assert get2 == "pw"
        assert len(sent) == 1

    asyncio.run(run())
