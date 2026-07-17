from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

app = FastAPI()

@app.get("/events")
def events():
    return StreamingResponse(iter(["id: 1\nevent: runtime\ndata: {\"type\":\"seed\"}\n\n"]), media_type="text/event-stream")

response = TestClient(app).get("/events")
assert response.status_code == 200
assert response.headers["content-type"].startswith("text/event-stream")
assert "event: runtime" in response.text and '"type":"seed"' in response.text
print("fastapi_sse=ok")
