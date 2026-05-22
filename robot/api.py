"""FastAPI web API for robot control."""
import logging
import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("api")

app = FastAPI(title="Trading Robot API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reference to robot instance (set by main.py)
_robot = None


def set_robot(robot):
    global _robot
    _robot = robot


@app.get("/status")
def status():
    if not _robot:
        return {"status": "not_initialized"}
    return _robot.get_status()


@app.post("/start")
def start():
    if not _robot:
        return {"error": "Robot not initialized"}
    if _robot._mode == "running":
        return {"status": "already_running"}
    threading.Thread(target=_robot.start, daemon=True).start()
    return {"status": "starting"}


@app.post("/stop")
def stop():
    if not _robot:
        return {"error": "Robot not initialized"}
    threading.Thread(target=lambda: _robot.stop(close_position=True), daemon=True).start()
    return {"status": "stopping"}


@app.post("/pause")
def pause():
    if not _robot:
        return {"error": "Robot not initialized"}
    _robot.pause()
    return {"status": "paused"}


@app.post("/resume")
def resume():
    if not _robot:
        return {"error": "Robot not initialized"}
    _robot.resume()
    return {"status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}
