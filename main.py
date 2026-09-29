import os
import requests
from fastapi import FastAPI
import uvicorn

app = FastAPI()


@app.get("/")
def home():
    token = os.getenv("BOT_TOKEN")

    if not token:
        return {
            "status": "ERROR",
            "message": "BOT_TOKEN environment variable is missing"
        }

    try:
        response = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={
                "Authorization": f"Bot {token}"
            },
            timeout=15
        )

        return {
            "status": "TEST COMPLETE",
            "discord_status_code": response.status_code,
            "discord_response": response.text[:1000]
        }

    except Exception as e:
        return {
            "status": "REQUEST ERROR",
            "error": str(e)
        }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
