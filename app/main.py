import os
import uuid
import boto3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="RAG Platform API")

AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
TABLE_NAME = os.getenv("TABLE_NAME", "documents")

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
