from flask import Flask
import os

# Flask app for Render health check
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot service is alive", 200

# Local testing entrypoint (not used on Render)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
