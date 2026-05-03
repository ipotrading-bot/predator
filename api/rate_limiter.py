import asyncio
import time
from functools import wraps

# Configuration: 15 requêtes par minute = 1 requête toutes les 4 secondes
rate_limit_delay = 4.0 
last_request_time = 0

def rate_limiter(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        global last_request_time
        elapsed = time.time() - last_request_time
        if elapsed < rate_limit_delay:
            wait_time = rate_limit_delay - elapsed
            await asyncio.sleep(wait_time)
        
        result = await func(*args, **kwargs)
        last_request_time = time.time()
        return result
    return wrapper
