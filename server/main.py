from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as redis
from contextlib import asynccontextmanager
from pydantic import BaseModel
import asyncio
from typing import Optional, List
from datetime import datetime, timedelta, timezone
import os
import json
import io
import unicodedata
import openpyxl
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
REDIS_URL = os.getenv("REDIS_URL")


RESULTS_CHANNEL = "live_results"  # worker.py finishes + time corrections
RUNNERS_CHANNEL = "runner_updates"  # runner created/edited (no time involved)

# Costa Rica no observa horario de verano, así que un offset fijo es
# correcto siempre — evita depender del paquete tzdata (ausente en la
# imagen python:3.12-slim) que necesitaría zoneinfo con un IANA tz name.
COSTA_RICA_TZ = timezone(timedelta(hours=-6))


def now_cr() -> datetime:
    """Hora actual en Costa Rica (UTC-6) como datetime naive. Todo el
    sistema guarda y compara timestamps 'naive' asumiendo que representan
    hora de Costa Rica (ver compute_elapsed, y el mismo criterio en
    admin/cliente) — esta es la única fuente de "ahora" del servidor, para
    que campos como received_at/corrected_at, generados con el reloj del
    servidor, queden en la misma zona horaria que las llegadas que recibe
    en vez de en la hora UTC real del sistema operativo."""
    return datetime.now(COSTA_RICA_TZ).replace(tzinfo=None)


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
    event_id: Optional[str] = None  # UUID para idempotencia (offline sync)


@app.post("/events")
async def receive_event(event: RawEvent, request: Request):
    doc = event.model_dump(mode="json")
    doc["received_at"] = now_cr().isoformat()

    # Always store the raw event first, no matter what happens next
    await request.app.mongodb["raw_events"].insert_one(
        {**doc, "received_at": now_cr()}
    )

    # Push to the queue for async processing instead of processing inline
    await request.app.redis_client.rpush("events_queue", json.dumps(doc))

    return {"status": "queued"}


class OfflineEvent(BaseModel):
    event_id: str
    runner_id: str
    timestamp: datetime


class OfflineSyncRequest(BaseModel):
    events: list[OfflineEvent]


@app.post("/events/sync")
async def sync_offline_events(request_body: OfflineSyncRequest, request: Request):
    """Processes offline events synchronously, one by one. Returns the
    status of each event so the client knows which were accepted,
    duplicated, or rejected due to conflicts."""
    db = request.app.mongodb
    results = []

    for event in request_body.events:
        result = await _process_offline_event(db, request.app.redis_client, event)
        results.append(result)

    return {"status": "ok", "results": results}


async def _process_offline_event(db, redis_client, event: OfflineEvent):
    """Processes a single offline event. Returns a status dict."""
    # 1. Check for duplicate event_id in results
    existing_result = await db["results"].find_one({"event_id": event.event_id})
    if existing_result is not None:
        return {"event_id": event.event_id, "status": "duplicate", "message": "Evento ya procesado"}

    # 2. Store raw event
    raw_doc = {
        "source": "offline",
        "runner_id": event.runner_id,
        "timestamp": event.timestamp,
        "event_id": event.event_id,
        "received_at": now_cr(),
    }
    await db["raw_events"].insert_one(raw_doc)

    # 3. Look up runner
    runner = await db["runners"].find_one({"runner_id": event.runner_id})
    if runner is None:
        return {"event_id": event.event_id, "status": "error", "message": f"Corredor {event.runner_id} no encontrado"}

    # 4. Check if runner already has a result
    existing = await db["results"].find_one({"runner_id": event.runner_id})
    finish_timestamp = event.timestamp
    if finish_timestamp.tzinfo is not None:
        finish_timestamp = finish_timestamp.replace(tzinfo=None)

    race_config = await db["race_config"].find_one({"category": runner["category"]})
    start_time = race_config["start_time"] if race_config is not None else None
    elapsed_seconds, elapsed_display = compute_elapsed(start_time, finish_timestamp)

    if existing is not None:
        # Conflict resolution: earlier finish time wins
        existing_ts = existing["timestamp"]
        if existing_ts is not None and finish_timestamp >= existing_ts:
            return {
                "event_id": event.event_id,
                "status": "conflict",
                "message": f"El corredor {event.runner_id} ya tiene un tiempo registrado con prioridad",
            }
        # Incoming event is earlier — replace existing result
        update_fields = {
            "timestamp": finish_timestamp,
            "elapsed_seconds": elapsed_seconds,
            "elapsed_display": elapsed_display,
            "source": "offline",
            "event_id": event.event_id,
            "corrected": True,
            "corrected_at": now_cr(),
        }
        await db["results"].update_one(
            {"runner_id": event.runner_id},
            {"$set": update_fields},
        )
        updated_doc = {**existing, **update_fields}
        updated_doc["_id"] = str(existing["_id"])
        await redis_client.publish(RESULTS_CHANNEL, json.dumps(updated_doc, default=str))
        return {"event_id": event.event_id, "status": "accepted"}

    # 5. No existing result — create new
    result_doc = {
        "runner_id": runner["runner_id"],
        "name": runner["name"],
        "gender": runner["gender"],
        "category": runner["category"],
        "subcategory": runner["subcategory"],
        "timestamp": finish_timestamp,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_display": elapsed_display,
        "source": "offline",
        "event_id": event.event_id,
    }
    insert_result = await db["results"].insert_one(result_doc)
    result_doc["_id"] = str(insert_result.inserted_id)
    await redis_client.publish(RESULTS_CHANNEL, json.dumps(result_doc, default=str))
    return {"event_id": event.event_id, "status": "accepted"}


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


@app.delete("/results")
async def delete_all_result_times(request: Request):
    """Clears every runner's recorded finish time in one shot — the bulk
    version of delete_result_time, used by the admin panel's 'Limpiar
    tiempos' action (both from Corredores and from Configuración de
    tiempos). Removes every 'results' document rather than nulling
    timestamps, for the same reason as the single-runner delete:
    worker.py's duplicate guard checks for document existence, not for a
    set timestamp — leaving blanked-out documents in place would block
    every runner from ever being recorded again. Emits one broadcast per
    cleared runner, same shape as an individual deletion, so connected
    clients revert them live."""

    cursor = request.app.mongodb["results"].find({})
    existing_results = await cursor.to_list(length=None)

    if not existing_results:
        return {"status": "ok", "cleared": 0}

    await request.app.mongodb["results"].delete_many({})

    for existing in existing_results:
        payload = {
            **existing,
            "_id": str(existing["_id"]),
            "timestamp": None,
            "elapsed_seconds": None,
            "elapsed_display": None,
            "corrected": False,
            "deleted": True,
        }
        await request.app.redis_client.publish(
            RESULTS_CHANNEL, json.dumps(payload, default=str)
        )

    return {"status": "ok", "cleared": len(existing_results)}


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


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def _normalize_text(value) -> str:
    return _strip_accents(str(value or "").strip().lower())


def _normalize_header(value) -> str:
    return _normalize_text(value)


# "Distancia" del formulario -> category del modelo Runner.
_CATEGORY_BY_DISTANCE = {
    "10km": "10K",
    "10 km": "10K",
    "5km": "5K",
    "5 km": "5K",
}

# "Categoria" (franja etaria) del formulario -> subcategory del modelo Runner.
_SUBCATEGORY_BY_LABEL = {
    "mayor": "mayor",
    "veterano": "veterano",
    "master": "master",
    "informatico": "informatico",
}

_GENDER_MAP = {
    "femenino": "F",
    "masculino": "M",
}


def _match_by_prefix(normalized_value: str, mapping: dict) -> Optional[str]:
    for key, mapped in mapping.items():
        if normalized_value.startswith(key):
            return mapped
    return None


def parse_runners_xlsx(file_bytes: bytes):
    """Parses the runner registration spreadsheet (Google Forms export)
    into a list of valid Runner objects plus a list of skipped rows with
    the reason each was skipped. Only the first sheet is read — extra
    sheets in the workbook (e.g. unrelated finance notes) are ignored.
    Rows are matched by header name rather than column position, since
    the form export carries several columns (email, cédula, teléfono,
    etc.) that aren't needed to build a Runner."""

    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)

    header = next(rows, None)
    if header is None:
        return [], []

    column_index = {}
    for i, cell in enumerate(header):
        if cell is None:
            continue
        column_index[_normalize_header(cell)] = i

    required_headers = ["numero", "nombre", "apellidos", "genero", "distancia"]
    missing = [h for h in required_headers if h not in column_index]
    if missing:
        raise ValueError(f"Faltan columnas requeridas en el archivo: {', '.join(missing)}")

    def cell(row, header_name):
        idx = column_index.get(header_name)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    runners = []
    skipped = []
    seen_runner_ids = set()

    for row_number, row in enumerate(rows, start=2):
        if row is None or all(v is None for v in row):
            continue

        numero = cell(row, "numero")
        if numero is None or str(numero).strip() == "":
            skipped.append({"row": row_number, "reason": "Falta el número de inscripción"})
            continue

        if isinstance(numero, (int, float)) and not isinstance(numero, bool):
            if isinstance(numero, float) and not numero.is_integer():
                skipped.append({"row": row_number, "reason": f"Número de inscripción no es numérico: {numero!r}"})
                continue
            runner_id = str(int(numero))
        else:
            numero_str = str(numero).strip()
            if not numero_str.isdigit():
                skipped.append({"row": row_number, "reason": f"Número de inscripción no es numérico: {numero!r}"})
                continue
            runner_id = numero_str

        if runner_id in seen_runner_ids:
            skipped.append({"row": row_number, "runner_id": runner_id, "reason": "runner_id duplicado en el archivo"})
            continue

        nombre = str(cell(row, "nombre") or "").strip()
        apellidos = str(cell(row, "apellidos") or "").strip()
        name = f"{nombre} {apellidos}".strip()
        if not name:
            skipped.append({"row": row_number, "runner_id": runner_id, "reason": "Falta el nombre"})
            continue

        gender = _GENDER_MAP.get(_normalize_text(cell(row, "genero")))
        if gender is None:
            skipped.append({"row": row_number, "runner_id": runner_id, "reason": f"Género no reconocido: {cell(row, 'genero')!r}"})
            continue

        category = _match_by_prefix(_normalize_text(cell(row, "distancia")), _CATEGORY_BY_DISTANCE)
        if category is None:
            skipped.append({"row": row_number, "runner_id": runner_id, "reason": f"Distancia no reconocida: {cell(row, 'distancia')!r}"})
            continue

        # subcategory (franja etaria) solo importa para el podio del 10K
        # (ver category.js en el admin) — un 5K siempre queda sin ella,
        # sin importar lo que traiga esa columna en el archivo.
        if category == "5K":
            subcategory = ""
        else:
            subcategory = _match_by_prefix(_normalize_text(cell(row, "categoria")), _SUBCATEGORY_BY_LABEL)
            if subcategory is None:
                skipped.append({"row": row_number, "runner_id": runner_id, "reason": f"Categoría no reconocida: {cell(row, 'categoria')!r}"})
                continue

        runners.append(
            Runner(
                runner_id=runner_id,
                tag_id=None,
                name=name,
                gender=gender,
                category=category,
                subcategory=subcategory,
            )
        )
        seen_runner_ids.add(runner_id)

    return runners, skipped


@app.post("/runners/bulk/replace-from-file")
async def replace_runners_bulk_from_file(request: Request, file: UploadFile = File(...)):
    """Replaces the ENTIRE runner roster from an uploaded .xlsx (the
    registration form export). Unlike /runners/bulk, which only adds
    runners and skips ones that already exist, this drops every current
    runner and every recorded result and rebuilds both from the file —
    used when a fresh/corrected registration export should become the
    new source of truth (e.g. right before race day). Rows the parser
    can't map to a valid Runner are skipped and reported back, same as
    /runners/bulk does for in-batch conflicts."""

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return {"status": "error", "message": "El archivo debe ser un .xlsx"}

    contents = await file.read()
    try:
        runners, skipped = parse_runners_xlsx(contents)
    except Exception as exc:
        return {"status": "error", "message": f"No se pudo leer el archivo: {exc}"}

    if not runners:
        return {
            "status": "error",
            "message": "El archivo no contiene corredores válidos",
            "skipped": skipped,
        }

    db = request.app.mongodb

    # Clear existing results first (same broadcast shape as
    # delete_all_result_times) so connected clients revert any live times
    # before the runner list itself changes underneath them.
    existing_results = await db["results"].find({}).to_list(length=None)
    if existing_results:
        await db["results"].delete_many({})
        for existing in existing_results:
            payload = {
                **existing,
                "_id": str(existing["_id"]),
                "timestamp": None,
                "elapsed_seconds": None,
                "elapsed_display": None,
                "corrected": False,
                "deleted": True,
            }
            await request.app.redis_client.publish(
                RESULTS_CHANNEL, json.dumps(payload, default=str)
            )

    await db["runners"].delete_many({})

    inserted = 0
    for runner in runners:
        await db["runners"].insert_one(runner.model_dump())
        await publish_runner_update(request, runner)
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
    """Sets or corrects the finish time of a runner. If a result already
    exists, corrects it in place (e.g. a wrong manual entry or a bad RFID
    read). If the runner has no result yet, creates one from scratch —
    this is what powers the admin panel's "Agregar tiempo" action for
    still-pending runners, through this same endpoint. Either way,
    recomputes the elapsed time from the category's start time and
    re-broadcasts so every connected client (admin panel, public client)
    picks up the change live, the same way a fresh finish would."""

    finish_timestamp = update.timestamp
    if finish_timestamp.tzinfo is not None:
        finish_timestamp = finish_timestamp.replace(tzinfo=None)

    existing = await request.app.mongodb["results"].find_one({"runner_id": runner_id})

    if existing is None:
        runner = await request.app.mongodb["runners"].find_one({"runner_id": runner_id})
        if runner is None:
            return {
                "status": "error",
                "message": f"El corredor {runner_id} no existe",
            }

        race_config = await request.app.mongodb["race_config"].find_one(
            {"category": runner["category"]}
        )
        start_time = race_config["start_time"] if race_config is not None else None
        elapsed_seconds, elapsed_display = compute_elapsed(start_time, finish_timestamp)

        new_doc = {
            "runner_id": runner["runner_id"],
            "name": runner["name"],
            "gender": runner["gender"],
            "category": runner["category"],
            "subcategory": runner["subcategory"],
            "timestamp": finish_timestamp,
            "elapsed_seconds": elapsed_seconds,
            "elapsed_display": elapsed_display,
            "source": "admin",
        }
        insert_result = await request.app.mongodb["results"].insert_one(new_doc)
        new_doc["_id"] = str(insert_result.inserted_id)

        await request.app.redis_client.publish(
            RESULTS_CHANNEL, json.dumps(new_doc, default=str)
        )

        return {"status": "ok", "result": new_doc}

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
        "corrected_at": now_cr(),
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


@app.delete("/results/{runner_id}/time")
async def delete_result_time(runner_id: str, request: Request):
    """Deletes a runner's recorded finish time, reverting them to
    pending. This removes the whole 'results' document rather than just
    nulling its timestamp: worker.py's duplicate guard checks whether a
    result document exists for the runner, not whether its timestamp is
    set, so leaving a blanked-out document in place would silently
    prevent that runner from ever being recorded again via a fresh RFID
    scan or manual entry. Re-broadcasts on the same channel/shape as a
    correction, with timestamp null and deleted=True, so connected
    clients revert the runner live and the admin activity log can label
    it correctly instead of reading it as a new finish."""

    existing = await request.app.mongodb["results"].find_one({"runner_id": runner_id})
    if existing is None:
        return {
            "status": "error",
            "message": f"No hay un resultado registrado para el corredor {runner_id}",
        }

    await request.app.mongodb["results"].delete_one({"runner_id": runner_id})

    payload = {
        **existing,
        "_id": str(existing["_id"]),
        "timestamp": None,
        "elapsed_seconds": None,
        "elapsed_display": None,
        "corrected": False,
        "deleted": True,
    }

    await request.app.redis_client.publish(
        RESULTS_CHANNEL, json.dumps(payload, default=str)
    )

    return {"status": "ok", "runner_id": runner_id}