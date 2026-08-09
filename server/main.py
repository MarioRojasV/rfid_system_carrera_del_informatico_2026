from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as redis
from contextlib import asynccontextmanager
from pydantic import BaseModel
import asyncio
from typing import Optional, List
from datetime import datetime
import os
import json
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
REDIS_URL = os.getenv("REDIS_URL")


async def listen_to_live_results():
    listener_redis = redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = listener_redis.pubsub()
    await pubsub.subscribe("live_results")
    print("Subscribed to live_results channel")

    async for message in pubsub.listen():
        if message["type"] == "message":
            await manager.broadcast(message["data"])


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
    return {"status": "ok", "runner_id": runner.runner_id}