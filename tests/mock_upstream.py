#!/usr/bin/env python3
"""Fake OpenAI upstream used by the end-to-end tests."""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()


def key_of(req: Request) -> str:
    return req.headers.get("authorization", "").replace("Bearer ", "")


def _fault(req: Request):
    k = key_of(req)
    if k.startswith("sk-bad"):
        return JSONResponse(status_code=401,
                            content={"error": {"message": "Incorrect API key", "code": "invalid_api_key"}})
    if k.startswith("sk-n404"):
        return JSONResponse(status_code=404,
                            content={"error": {"message": "The model does not exist", "code": "model_not_found"}})
    return None


@app.get("/v1/models")
async def models():
    return {"data": [{"id": m} for m in
                     ["gpt-6-sol", "gpt-5.6-terra", "gpt-7-nova", "gpt-image-3", "gpt-5.6-luna"]]}


@app.post("/v1/responses")
async def responses(req: Request):
    g = _fault(req)
    if g:
        return g
    body = await req.json()
    n_in, n_out = 1000, 200
    if body.get("stream"):
        async def gen():
            yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
            yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            done = {"type": "response.completed",
                    "response": {"usage": {"input_tokens": n_in, "output_tokens": n_out}}}
            yield f"event: response.completed\ndata: {json.dumps(done)}\n\n".encode()
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"id": "resp_1", "model": body.get("model"),
            "usage": {"input_tokens": n_in, "output_tokens": n_out}}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    g = _fault(req)
    if g:
        return g
    body = await req.json()
    if body.get("stream"):
        if not (body.get("stream_options") or {}).get("include_usage"):
            return JSONResponse(status_code=400,
                                content={"error": {"message": "stream_options.include_usage missing"}})

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            tail = {"choices": [], "usage": {"prompt_tokens": 500, "completion_tokens": 50}}
            yield f"data: {json.dumps(tail)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"usage": {"prompt_tokens": 500, "completion_tokens": 50}}
