import asyncio
import logging

logging.basicConfig(level=logging.INFO)

from services.llm_service import get_llm_service

async def test():
    svc = get_llm_service()
    try:
        res = await svc._call_api('system', 'user')
        print(res)
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
