import asyncio


async def fast():
    await asyncio.sleep(0.01)
    return 40


async def slow():
    await asyncio.sleep(0.02)
    raise TimeoutError("upstream timed out")


async def main():
    results = await asyncio.gather(fast(), slow(), return_exceptions=True)
    return results
