import os

from transit_od import create_app

app = create_app(os.environ.get("APP_DB"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True)
