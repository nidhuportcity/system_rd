# Hybrid RAG API setup

The service uses Docling, BGE-M3 dense embeddings, Qdrant BM25, RRF fusion,
`BAAI/bge-reranker-v2-m3`, and local Ollama (`qwen3:8b`) for answer generation.

## Run everything with Docker Compose

Copy the environment template and add your Hugging Face token. Do not put the token
directly in `compose.yaml`.

```powershell
Copy-Item .env.example .env
notepad .env
```

Docker Compose starts the FastAPI service and Qdrant. The first build and request
can take a while because the Python dependencies and local retrieval models are large.

```powershell
docker compose up --build
```

Run it in the background instead:

```powershell
docker compose up --build -d
docker compose logs -f api
```

After startup, open `http://localhost:8000/docs`. Qdrant's dashboard is available
at `http://localhost:6333/dashboard`.

Stop the services without deleting indexed documents or downloaded models:

```powershell
docker compose down
```

The named volumes `qdrant_storage` and `model_cache` preserve indexed documents and
downloaded retrieval models. Using `docker compose down -v` deletes both volumes.

## Install

Python 3.10 or 3.11 is recommended. Create/activate the virtual environment, then:

```powershell
python -m pip install -r requirements.txt
```

The existing `/ocr` endpoint separately requires PaddlePaddle:

```powershell
python -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
```

## Start services

Start Qdrant (1.15 or newer):

```powershell
docker run --name rag-qdrant -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant:latest
```

Start Ollama locally and make sure the model is available (`ollama pull qwen3:8b`
if you have not downloaded it). Configure the API:

```powershell
$env:OLLAMA_HOST = "http://localhost:11434"
$env:LLM_MODEL = "qwen3:8b"
```

Start the API:

```powershell
.\.venv1\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

The first request downloads the embedding, reranking, BM25, and Docling model files.

## Endpoints

- `POST /documents`: multipart upload using field `file`; returns a `document_id`.
- `POST /search`: hybrid dense + BM25 retrieval, RRF fusion, and BGE reranking.
- `POST /ask`: retrieval plus grounded LLM answer and source passages.
- `DELETE /documents/{document_id}`: remove every chunk from one upload.
- `GET /health`: lightweight liveness check.

Example:

```powershell
curl.exe -X POST "http://localhost:8000/documents" -F "file=@manual.pdf"
curl.exe -X POST "http://localhost:8000/ask" -H "Content-Type: application/json" -d '{"query":"How do I reset the device?","limit":5}'
```

Add `document_id` to a `/search` or `/ask` JSON body to scope retrieval to one file.

## Configuration

| Variable | Default |
|---|---|
| `QDRANT_URL` | `http://localhost:6333` |
| `QDRANT_API_KEY` | unset |
| `QDRANT_COLLECTION` | `rag_documents` |
| `DENSE_MODEL` | `BAAI/bge-m3` |
| `DENSE_DIMENSION` | `1024` |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` |
| `OLLAMA_HOST` | `http://localhost:11434` |
| `LLM_MODEL` | `qwen3:8b` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1200` / `200` characters |
| `MAX_DOCUMENT_BYTES` | `52428800` (50 MB) |

Answer generation requires a running Ollama server, not an OpenAI or Hugging Face
API key. Docker Compose uses `host.docker.internal:11434` to reach Ollama on the
host; Ollama must be reachable from the container. On native Linux, configure the
host gateway separately. Embedding and reranker model downloads still use Hugging Face.
