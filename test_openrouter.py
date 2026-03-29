import os
import asyncio
import aiohttp
import json
from dotenv import load_dotenv

load_dotenv()

async def test():
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}", "Content-Type": "application/json"}
        json_data = {'model': 'google/gemini-3.1-flash-image-preview', 'messages': [{'role': 'user', 'content': 'red apple'}]}
        async with session.post('https://openrouter.ai/api/v1/chat/completions', headers=headers, json=json_data) as resp:
            data = await resp.json()
            print(json.dumps(data, indent=2))

asyncio.run(test())
