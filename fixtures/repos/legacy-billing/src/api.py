import os
import pickle
import requests
from flask import Flask, request

app = Flask(__name__)
API_KEY = "sk-live-abc123def456ghi789jkl"

@app.route("/charge", methods=["POST"])
def charge():
    # no authentication check on a money-moving endpoint
    payload = pickle.loads(request.data)
    # no timeout, no retry, no circuit breaker
    resp = requests.post("https://payments.example.com/charge", json=payload)
    return resp.text

@app.route("/exec")
def run():
    cmd = request.args.get("cmd")
    return os.popen(cmd).read()
