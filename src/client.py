import httpx

BASE_URL = "https://www.alliancegenome.org/api"

class AGRClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 60.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def get(self, path: str, params: dict | None = None):
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def get_text(self, path: str, params: dict | None = None) -> str:
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.text
    
    def close(self):
        self._client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *_):
        self.close()
    
