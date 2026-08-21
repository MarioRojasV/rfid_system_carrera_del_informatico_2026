import asyncio
import json
import os
from datetime import datetime, timedelta
from pymongo.errors import DuplicateKeyError
import redis.asyncio as redis
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
REDIS_URL = os.getenv("REDIS_URL")


async def process_event(db, event: dict, redis_client):
    tag_id = event.get("tag_id")
    runner_id = event.get("runner_id")

    if tag_id is not None:
        runner = await db["runners"].find_one({"tag_id": tag_id})
    elif runner_id is not None:
        runner = await db["runners"].find_one({"runner_id": runner_id})
    else:
        print("Skipped event: no tag_id or runner_id")
        return

    if runner is None:
        identifier = tag_id or runner_id
        print(f"No runner found for {identifier}")
        return

    resolved_runner_id = runner["runner_id"]

    # Idempotency check: skip if this event_id was already processed
    event_id = event.get("event_id")
    if event_id:
        already_processed = await db["results"].find_one({"event_id": event_id})
        if already_processed:
            print(f"Duplicate event_id ignored: {event_id}")
            return

    existing = await db["results"].find_one({"runner_id": resolved_runner_id})
    if existing is not None:
        print(f"Duplicate ignored for runner {resolved_runner_id}")
        return

    finish_timestamp = datetime.fromisoformat(event["timestamp"])
    if finish_timestamp.tzinfo is not None:
        finish_timestamp = finish_timestamp.replace(tzinfo=None)

    race_config = await db["race_config"].find_one({"category": runner["category"]})
    elapsed_seconds = None
    elapsed_display = None
    if race_config is None:
        print(f"No start time configured for category {runner['category']}, saving without elapsed time")
    else:
        start_time = race_config["start_time"]
        if start_time.tzinfo is not None:
            start_time = start_time.replace(tzinfo=None)
        elapsed_seconds = (finish_timestamp - start_time).total_seconds()
        elapsed_display = str(timedelta(seconds=int(elapsed_seconds)))

    result_doc = {
        "runner_id": resolved_runner_id,
        "name": runner["name"],
        "gender": runner["gender"],
        "category": runner["category"],
        "subcategory": runner["subcategory"],
        "timestamp": finish_timestamp,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_display": elapsed_display,
        "source": event["source"],
        "event_id": event_id,
    }
    try:
        await db["results"].insert_one(result_doc)
    except DuplicateKeyError:
        print(f"Duplicate write retry ignored for runner {resolved_runner_id}")
        return

    print(f"Recorded time for runner {resolved_runner_id}: {elapsed_display}")
    await redis_client.publish("live_results", json.dumps(result_doc, default=str))


async def main():
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    db = mongo_client[MONGO_DB_NAME]

    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10)

    print("Worker started, waiting for events...")

    while True:
        try:
            # Blocks until an event is available, checks every 5 seconds otherwise
            item = await redis_client.blpop("events_queue", timeout=5)
        except redis.exceptions.TimeoutError:
            # Known redis-py quirk: sometimes raises TimeoutError instead of
            # returning None when the blocking timeout expires with no data
            continue

        if item is None:
            continue

        _, raw_event = item
        event = json.loads(raw_event)
        await process_event(db, event, redis_client)


if __name__ == "__main__":
    asyncio.run(main())