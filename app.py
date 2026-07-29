from __future__ import annotations

import os

from application import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("APP_PORT", "5100")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
