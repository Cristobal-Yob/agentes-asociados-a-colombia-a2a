import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "demo_ui.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8110")),
        access_log=False,
    )
