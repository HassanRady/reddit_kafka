from fastapi import FastAPI
import uvicorn

from src.reddit_kafka_pipeline import stream_subreddit_producer_loop
from src.api_exception import RequestError, RequestErrorHandler


app = FastAPI()


@app.exception_handler(RequestError)
async def request_error_internal(request, exc):
    reh = RequestErrorHandler(exc)
    return reh.process_message()


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/health")
async def health():
    return {"message": "OK"}


@app.get("/stream")
async def stream(subreddit: str):
    await stream_subreddit_producer_loop(subreddit)
    return {"message": "stream stopped"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
