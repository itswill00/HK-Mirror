import asyncio
from typing import Dict, Any

# Global trackers for active tasks (cancellation)
active_tasks: Dict[str, asyncio.Task] = {}
active_tasks_meta: Dict[str, Dict[str, Any]] = {}
cooldowns: Dict[int, float] = {}
mirror_queries: Dict[str, Dict[str, Any]] = {}
