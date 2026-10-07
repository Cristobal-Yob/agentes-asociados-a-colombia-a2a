import logging
import os

import uvicorn


def run(agent, default_port):
    # Never allow inherited tracing settings to export classroom content.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ["httpx", "httpcore", "httpx2", "a2a", "langsmith"]:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    uvicorn.run(
        f"{agent}_agent.app:create_app",
        factory=True,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", str(default_port))),
        access_log=False,
        log_level="warning",
    )
