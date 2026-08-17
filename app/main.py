import os
import uuid
import boto3
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="RAG Platform API")

AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
TABLE_NAME = os.getenv("TABLE_NAME", "documents")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral")

dynamodb = boto3.resource(
    "dynamodb",
    endpoint_url=AWS_ENDPOINT_URL,
    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
)
table = dynamodb.Table(TABLE_NAME)


class Document(BaseModel):
    title: str
    content: str


class Query(BaseModel):
    question: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/documents")
def create_document(doc: Document):
    doc_id = str(uuid.uuid4())
    table.put_item(Item={"id": doc_id, "title": doc.title, "content": doc.content})
    return {"id": doc_id, **doc.dict()}


@app.get("/documents")
def list_documents():
    try:
        response = table.scan()
        return response.get("Items", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query")
def query(q: Query):
    try:
        items = table.scan().get("Items", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"document store error: {e}")

    context = "\n\n".join(f"- {item['title']}: {item['content']}" for item in items)
    prompt = (
        "Answer the question using only the context below. "
        "If the context doesn't contain the answer, say so.\n\n"
        f"Context:\n{context}\n\nQuestion: {q.question}\nAnswer:"
    )

    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ollama error: {e}")

    return {"answer": resp.json().get("response", "").strip(), "documents_used": len(items)}
