import httpx

class PaymentsClient:
    def __init__(self, base_url: str, timeout_s: float = 5.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)

    def charge(self, payload: dict) -> dict:
        # bounded timeout; retries handled by the caller's policy
        response = self._client.post("/charge", json=payload)
        response.raise_for_status()
        return response.json()
