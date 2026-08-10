from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as redis
from contextlib import asynccontextmanager
from pydantic import BaseModel
import asyncio
from typing import Optional, List
from datetime import datetime, timedelta
import os
import json
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
REDIS_URL = os.getenv("REDIS_URL")


RESULTS_CHANNEL = "live_results"  # worker.py finishes + time corrections
RUNNERS_CHANNEL = "runner_updates"  # runner created/edited (no time involved)


async def listen_to_live_results():
    listener_redis = redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = listener_redis.pubsub()
    await pubsub.subscribe(RESULTS_CHANNEL, RUNNERS_CHANNEL)
    print(f"Subscribed to {RESULTS_CHANNEL} and {RUNNERS_CHANNEL} channels")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue

        # Tag each broadcast with where it came from so clients can tell a
        # "runner created/edited" update apart from an actual finish time —
        # the assistant app in particular treats every message as "this
        # runner just finished" and must not confuse the two.
        try:
            payload = json.loads(message["data"])
        except (TypeError, ValueError):
            continue
        payload.setdefault(
            "type", "result" if message["channel"] == RESULTS_CHANNEL else "runner"
        )
        await manager.broadcast(json.dumps(payload, default=str))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs on startup
    app.mongodb_client = AsyncIOMotorClient(MONGO_URL)
    app.mongodb = app.mongodb_client[MONGO_DB_NAME]
    print(f"Connected to MongoDB database: {MONGO_DB_NAME}")

    app.redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10)
    print("Connected to Redis")

    listener_task = asyncio.create_task(listen_to_live_results())

    yield

    # Runs on shutdown
    listener_task.cancel()

    app.mongodb_client.close()
    print("MongoDB connection closed")

    await app.redis_client.aclose()
    print("Redis connection closed")


app = FastAPI(lifespan=lifespan)

# TODO: restrict to specific origins once the client domains are known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)


manager = ConnectionManager()


def compute_elapsed(start_time: Optional[datetime], finish_timestamp: datetime):
    """Mirrors the elapsed time calculation done by worker.py when a result
    is first recorded, so corrections stay consistent with normal processing."""
    if start_time is None:
        return None, None

    if start_time.tzinfo is not None:
        start_time = start_time.replace(tzinfo=None)

    elapsed_seconds = (finish_timestamp - start_time).total_seconds()
    elapsed_display = str(timedelta(seconds=int(elapsed_seconds)))
    return elapsed_seconds, elapsed_display


@app.get("/health")
def health():
    return {"status": "ok"}


class RawEvent(BaseModel):
    source: str  # "rfid" or "manual"
    tag_id: Optional[str] = None
    runner_id: Optional[str] = None
    timestamp: datetime


@app.post("/events")
async def receive_event(event: RawEvent, request: Request):
    doc = event.model_dump(mode="json")
    doc["received_at"] = datetime.utcnow().isoformat()

    # Always store the raw event first, no matter what happens next
    await request.app.mongodb["raw_events"].insert_one(
        {**doc, "received_at": datetime.utcnow()}
    )

    # Push to the queue for async processing instead of processing inline
    await request.app.redis_client.rpush("events_queue", json.dumps(doc))

    return {"status": "queued"}


@app.get("/results")
async def get_results(
    request: Request,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    gender: Optional[str] = None,
):
    query = {}
    if category is not None:
        query["category"] = category
    if subcategory is not None:
        query["subcategory"] = subcategory
    if gender is not None:
        query["gender"] = gender

    cursor = request.app.mongodb["results"].find(query).sort("timestamp", 1)
    results = await cursor.to_list(length=None)

    for r in results:
        r["_id"] = str(r["_id"])

    return {"count": len(results), "results": results}


class RaceConfig(BaseModel):
    category: str
    start_time: datetime


@app.post("/race-config")
async def set_race_config(config: RaceConfig, request: Request):
    await request.app.mongodb["race_config"].update_one(
        {"category": config.category},
        {"$set": {"start_time": config.start_time}},
        upsert=True,
    )
    return {"status": "ok", "category": config.category, "start_time": config.start_time}


@app.get("/race-config")
async def get_race_config(request: Request):
    cursor = request.app.mongodb["race_config"].find({})
    configs = await cursor.to_list(length=None)
    for c in configs:
        c["_id"] = str(c["_id"])
    return {"configs": configs}


@app.websocket("/ws/live")
async def websocket_live_results(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # We don't expect messages from the client, just keep the connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


class Runner(BaseModel):
    runner_id: str
    tag_id: Optional[str] = None
    name: str
    gender: str
    category: str
    subcategory: str


async def publish_runner_update(request: Request, runner: Runner):
    """Broadcasts a runner creation/edit over the websocket. Deliberately
    carries no timestamp field at all (not even null) so clients that merge
    by runner_id don't clobber a finish time already recorded for them."""
    await request.app.redis_client.publish(
        RUNNERS_CHANNEL, json.dumps(runner.model_dump(), default=str)
    )


@app.post("/runners")
async def create_runner(runner: Runner, request: Request):
    existing = await request.app.mongodb["runners"].find_one(
        {"runner_id": runner.runner_id}
    )
    if existing is not None:
        return {"status": "error", "message": f"Runner {runner.runner_id} already exists"}

    if runner.tag_id is not None:
        tag_collision = await request.app.mongodb["runners"].find_one(
            {"tag_id": runner.tag_id}
        )
        if tag_collision is not None:
            return {
                "status": "error",
                "message": f"tag_id {runner.tag_id} is already assigned to runner {tag_collision['runner_id']}"
            }

    await request.app.mongodb["runners"].insert_one(runner.model_dump())
    await publish_runner_update(request, runner)
    return {"status": "ok", "runner_id": runner.runner_id}


@app.post("/runners/bulk")
async def create_runners_bulk(runners: list[Runner], request: Request):
    inserted = 0
    skipped = []
    seen_runner_ids = set()
    seen_tag_ids = set()

    for runner in runners:
        if runner.runner_id in seen_runner_ids:
            skipped.append({"runner_id": runner.runner_id, "reason": "duplicated in this batch"})
            continue

        if runner.tag_id is not None and runner.tag_id in seen_tag_ids:
            skipped.append({"runner_id": runner.runner_id, "reason": f"tag_id {runner.tag_id} duplicated in this batch"})
            continue

        existing = await request.app.mongodb["runners"].find_one(
            {"runner_id": runner.runner_id}
        )
        if existing is not None:
            skipped.append({"runner_id": runner.runner_id, "reason": "runner_id already exists"})
            continue

        if runner.tag_id is not None:
            tag_collision = await request.app.mongodb["runners"].find_one(
                {"tag_id": runner.tag_id}
            )
            if tag_collision is not None:
                skipped.append({
                    "runner_id": runner.runner_id,
                    "reason": f"tag_id {runner.tag_id} already assigned to runner {tag_collision['runner_id']}"
                })
                continue

        await request.app.mongodb["runners"].insert_one(runner.model_dump())
        await publish_runner_update(request, runner)
        seen_runner_ids.add(runner.runner_id)
        if runner.tag_id is not None:
            seen_tag_ids.add(runner.tag_id)
        inserted += 1

    return {"status": "ok", "inserted": inserted, "skipped": skipped}


@app.get("/runners")
async def get_runners(request: Request):
    cursor = request.app.mongodb["runners"].find({})
    runners = await cursor.to_list(length=None)
    for r in runners:
        r["_id"] = str(r["_id"])
    return {"count": len(runners), "runners": runners}


@app.put("/runners/{runner_id}")
async def update_runner(runner_id: str, runner: Runner, request: Request):
    existing = await request.app.mongodb["runners"].find_one({"runner_id": runner_id})
    if existing is None:
        return {"status": "error", "message": f"Runner {runner_id} not found"}

    # Check if the new runner_id (if changed) collides with another runner
    if runner.runner_id != runner_id:
        collision = await request.app.mongodb["runners"].find_one(
            {"runner_id": runner.runner_id}
        )
        if collision is not None:
            return {
                "status": "error",
                "message": f"runner_id {runner.runner_id} is already used by another runner"
            }

    # Check if the tag_id (if set) collides with another runner
    if runner.tag_id is not None:
        collision = await request.app.mongodb["runners"].find_one(
            {"tag_id": runner.tag_id, "runner_id": {"$ne": runner_id}}
        )
        if collision is not None:
            return {
                "status": "error",
                "message": f"tag_id {runner.tag_id} is already assigned to runner {collision['runner_id']}"
            }

    await request.app.mongodb["runners"].update_one(
        {"runner_id": runner_id},
        {"$set": runner.model_dump()},
    )
    await publish_runner_update(request, runner)
    return {"status": "ok", "runner_id": runner.runner_id}


class ResultTimeUpdate(BaseModel):
    timestamp: datetime


@app.put("/results/{runner_id}/time")
async def update_result_time(runner_id: str, update: ResultTimeUpdate, request: Request):
    """Corrects the finish time of a runner that already has a recorded
    result (e.g. a wrong manual entry or a bad RFID read). Recomputes the
    elapsed time from the category's start time and re-broadcasts the
    result so every connected client (admin panel, public client) picks up
    the correction live, the same way a fresh finish would."""

    existing = await request.app.mongodb["results"].find_one({"runner_id": runner_id})
    if existing is None:
        return {
            "status": "error",
            "message": f"No hay un resultado registrado para el corredor {runner_id}",
        }

    finish_timestamp = update.timestamp
    if finish_timestamp.tzinfo is not None:
        finish_timestamp = finish_timestamp.replace(tzinfo=None)

    race_config = await request.app.mongodb["race_config"].find_one(
        {"category": existing["category"]}
    )
    start_time = race_config["start_time"] if race_config is not None else None
    elapsed_seconds, elapsed_display = compute_elapsed(start_time, finish_timestamp)

    update_fields = {
        "timestamp": finish_timestamp,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_display": elapsed_display,
        "corrected": True,
        "corrected_at": datetime.utcnow(),
    }

    await request.app.mongodb["results"].update_one(
        {"runner_id": runner_id},
        {"$set": update_fields},
    )

    updated_doc = {**existing, **update_fields}
    updated_doc["_id"] = str(existing["_id"])

    # Same channel/shape used by worker.py so existing listeners (websocket
    # clients merging by runner_id) pick up the correction without changes.
    await request.app.redis_client.publish(
        RESULTS_CHANNEL, json.dumps(updated_doc, default=str)
    )

    return {"status": "ok", "result": updated_doc}