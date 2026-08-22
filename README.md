# RFID Sistema · Carrera del Informático 2026

Backend del sistema de cronometraje por RFID: API HTTP + WebSocket
(FastAPI), un worker que procesa las lecturas RFID/manuales de forma
asíncrona, MongoDB como base de datos y Redis como cola de eventos +
canal de pub/sub para las actualizaciones en vivo.

Lo consumen las tres apps del frontend (`cliente`, `administrador`,
`podio` — [repo `cliente-administrador-RFID`](https://github.com/sandiblanco/cliente-administrador-RFID),
donde este repo está incluido como submódulo).

## Arquitectura

```
lector RFID / entrada manual
        │
        ▼
  POST /events  ──────────►  raw_events (Mongo, log de todo lo recibido)
        │                          │
        ▼                          │ se guarda siempre, pase lo que pase
  Redis: events_queue (rpush)      │
        │                          │
        ▼                          ▼
    worker.py (blpop, uno         resultado final:
    a la vez, idempotente         "results" (Mongo)
    por event_id y por
    runner_id)
        │
        ▼
  Redis pub/sub: live_results ──► WebSocket /ws/live ──► cliente/administrador/podio
```

`raw_events`/`events_queue` desacoplan la ingesta (rápida, nunca se
pierde una lectura) del procesamiento (que necesita resolver el
corredor, chequear duplicados y calcular el tiempo transcurrido). Un
corredor creado/editado desde el administrador (`runner_updates`,
canal separado) se retransmite por el mismo WebSocket, etiquetado con
`type: "runner"` en vez de `type: "result"`, para que los clientes no lo
confundan con una llegada real.

## Requisitos

- Python 3.12+
- MongoDB (4.4 en el `docker-compose.yml` — pineado por compatibilidad
  con NAS/CPUs sin AVX; cualquier 4.4+ sirve para desarrollo)
- Redis (7 en el `docker-compose.yml`; cualquier 6+ sirve)
- Docker + Docker Compose (opcional, para levantar todo junto)

## Desarrollo local (sin Docker)

Con Mongo y Redis corriendo en algún lado (local, Docker suelto, etc.):

```sh
cd server
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Editar .env si Mongo/Redis no están en localhost con los puertos default

uvicorn main:app --reload --port 8000
```

Y en otra terminal, el worker (procesa la cola — sin esto, los eventos
se encolan pero nunca se convierten en resultados):

```sh
cd server
source .venv/bin/activate
python worker.py
```

### Variables de entorno (`server/.env`)

| Variable | Ejemplo | Para qué |
|---|---|---|
| `MONGO_URL` | `mongodb://localhost:27017` | Conexión a MongoDB |
| `MONGO_DB_NAME` | `carrera_informatico_2026` | Base de datos a usar |
| `REDIS_URL` | `redis://localhost:6379` | Cola de eventos + pub/sub en vivo |

Ver `server/.env.example` como punto de partida.

## Docker (todo el stack: mongo + redis + server + worker)

```sh
docker compose up -d --build
```

Levanta cuatro contenedores: `carrera-mongo`, `carrera-redis`,
`carrera-server` (API, puerto `8000`) y `carrera-worker`. Las variables
de entorno para `server`/`worker` ya están fijadas en el
`docker-compose.yml` apuntando a los contenedores de mongo/redis por su
nombre de servicio — no hace falta `.env` para este modo.

Este stack se une, además de a la red default, a una red externa
`rfid-net` — compartida con el `docker-compose.yml` del frontend
(repo `cliente-administrador-RFID`), para que el Nginx de cada app
frontend pueda hacer `proxy_pass` hacia `carrera-server` sin depender de
ninguna IP de LAN/Tailscale:

```sh
docker network create rfid-net   # una sola vez, si no existe ya
```

`deploy.sh` la crea automáticamente si hace falta.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Chequeo de salud |
| `POST` | `/events` | Registra una lectura (RFID o manual); encola el procesamiento, no responde con el resultado |
| `POST` | `/events/sync` | Reenvía en lote eventos guardados offline (ver cliente); resuelve duplicados/conflictos de una vez, sincrónico |
| `GET` | `/results` | Lista de resultados (filtros: `category`, `subcategory`, `gender`) |
| `DELETE` | `/results` | Borra **todos** los tiempos registrados (vuelve todo a pendiente) |
| `POST`/`GET` | `/race-config` | Fija/consulta la hora de inicio de una categoría (5K/10K) |
| `WS` | `/ws/live` | Resultados y altas/ediciones de corredores en vivo |
| `POST` | `/runners` | Crea un corredor |
| `POST` | `/runners/bulk` | Alta masiva (agrega, no reemplaza; salta los que ya existen) |
| `POST` | `/runners/bulk/replace-from-file` | Reemplaza TODO el listado desde un `.xlsx` de inscripción — ver detalle abajo |
| `GET` | `/runners` | Lista de corredores |
| `PUT` | `/runners/{runner_id}` | Actualiza un corredor (documento completo, no un parche) |
| `PUT`/`DELETE` | `/results/{runner_id}/time` | Corrige/borra el tiempo final de un corredor puntual |

### Reemplazo de corredores desde `.xlsx`

`POST /runners/bulk/replace-from-file` toma el export del formulario de
inscripción (Google Forms), reemplaza **todo** el listado de corredores
y borra los tiempos ya registrados. Columnas requeridas (por nombre de
header, no por posición): número, nombre, apellidos, género, distancia;
la categoría (franja etaria) solo es obligatoria para 10K — un 5K
siempre queda sin ella. La talla de camiseta se detecta por cualquier
header que contenga "talla" y es opcional.

Campos que **sobreviven** al reemplazo (se reaplican por `runner_id`
después de reimportar, porque el archivo de inscripción no los trae):
`tag_id`, `shirt_delivered`, `kit_delivered`. `shirt_size`, en cambio,
**sí** se toma siempre del archivo nuevo, igual que nombre/género/categoría.

Las filas que no se puedan mapear a un corredor válido se reportan
(fila, motivo y nombre del corredor cuando se puede identificar) en vez
de bloquear la subida entera.

## Despliegue

`./deploy.sh` sincroniza `origin/main` en el NAS de la carrera (detecta
si es accesible por LAN o por Tailscale, hace `git reset --hard` al
remoto, reconstruye los cuatro contenedores y verifica `/health` — tanto
directo al NAS como a través del reverse proxy, cuando aplica). Requiere
`sshpass` instalado y las credenciales SSH del NAS. Ver el script para
el detalle de hosts/puertos.

```sh
./deploy.sh
```
