"""Entry point: `python app.py` (the launchd service runs this). The app itself is built by
app_factory.create_app so tests and tools can build one without starting background threads."""

import os

from app_factory import create_app

app = create_app()


if __name__ == "__main__":
    # Debug is off by default: once `tailscale serve` puts this in front of
    # your tailnet, Werkzeug's interactive debugger would be reachable from
    # other devices, and it can execute code. Tracebacks still go to the
    # terminal/log either way. Set AUTO_APPLY_DEBUG=true for the in-browser
    # debugger while working locally.
    debug = os.environ.get("AUTO_APPLY_DEBUG", "").lower() == "true"
    port = int(os.environ.get("PORT", "8000"))
    app.run(debug=debug, use_reloader=False, port=port)
