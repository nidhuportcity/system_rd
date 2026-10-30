# FastAPI PaddleOCR service

Upload an image and receive detected text, confidence scores, and bounding boxes.

## Setup (CPU)

Python 3.10 or 3.11 is recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
python -m pip install -r requirements.txt
```

## Run

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`, or upload an image directly:

```powershell
curl.exe -X POST "http://localhost:8000/ocr" -F "file=@sample.png"
```

The first OCR request may download PaddleOCR model files. Only JPEG, PNG, WebP, BMP, and TIFF images up to 10 MB are accepted. Set `OCR_LANG` (default `en`) or `MAX_UPLOAD_BYTES` to change those settings.

To also save the extracted text to Qdrant, use `POST /ocr?index=true`:

```powershell
curl.exe -X POST "http://localhost:8000/ocr?index=true" -F "file=@sample_ocr.png"
```

This uses the same chunking, dense embeddings, and BM25 indexing as `/documents`,
preserves the uploaded filename, and returns `document_id` and `chunks_indexed`
alongside the OCR text and lines. Use that ID with `/search`, `/ask`, or
`DELETE /documents/{document_id}`. Qdrant and the embedding models must be available.
Only extracted text and document metadata are indexed; the image itself is not saved.
Without `index=true`, OCR only extracts text and does not access Qdrant.
An image with no extracted text returns 422 when indexing is requested.
Each indexed upload creates a new document ID, including repeated uploads.

## Test

```powershell
python -m pip install -r requirements-dev.txt
pytest
```

Tests mock the OCR engine and do not download a model.
