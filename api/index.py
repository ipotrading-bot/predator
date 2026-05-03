from flask import Flask, jsonify
import logging
import sys

# Configure logging to see output in Vercel logs
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    logger.info(f"Request received for path: {path}")
    return jsonify({"message": "Predator API is running"}), 200

# DO NOT include app.run() in serverless functions!
# The handler is just the app object.
