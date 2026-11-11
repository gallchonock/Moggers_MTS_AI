"""OpenAI-compatible chat client for the configured competition proxy."""

import json
from threading import Event, Lock
from time import monotonic, sleep

import httpx


class ProxyError(Exception):
    def __init__(self, kind: str, status: int | None = None):
        super().__init__(kind if status is None else f"{kind}:{status}")
        self.kind = kind
        self.status = status


class Proxy:
    def __init__(
        self, url: str, key: str, model: str, temperature: float, max_tokens: int,
        deadline: float, max_workers: int,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.deadline = deadline
        self.auth_failed = Event()
        self.rerank_auth_failed = Event()
        self.lock = Lock()
        self.requests = 0
        self.retries = 0
        self.throttled = 0
        self.cooldown_until = 0.0
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + key},
            limits=httpx.Limits(
                max_connections=max_workers, max_keepalive_connections=max_workers,
            ),
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def chat(self, messages: list[dict[str, str]]) -> str:
        result = self._post("chat/completions", {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        })
        try:
            answer = result["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProxyError("invalid_chat_response") from None
        return answer

    def _post(self, endpoint: str, payload: dict) -> dict:
        for attempt in range(2):
            if self._auth_failed(endpoint):
                raise ProxyError("authentication", 401)
            with self.lock:
                pause = max(0.0, self.cooldown_until - monotonic())
            if pause:
                self._pause(pause)
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise ProxyError("deadline")
            with self.lock:
                if self._auth_failed(endpoint):
                    raise ProxyError("authentication", 401)
                self.requests += 1
                self.retries += attempt
            try:
                return self._request(endpoint, payload, remaining)
            except httpx.TransportError:
                error = ProxyError("transport")
            except ProxyError as exc:
                error = exc
                if error.status != 429 and not (error.status and 500 <= error.status <= 599):
                    raise
            if attempt:
                raise error
            self._pause(0.3)
        raise AssertionError("retry loop did not return")

    def _auth_failed(self, endpoint: str) -> bool:
        return self.auth_failed.is_set() or (
            endpoint == "rerank" and self.rerank_auth_failed.is_set()
        )

    def _pause(self, seconds: float) -> None:
        if self.deadline - monotonic() <= seconds:
            raise ProxyError("deadline")
        sleep(seconds)

    def _request(self, endpoint: str, payload: dict, remaining: float) -> dict:
        limit = min(remaining, 60.0)
        timeout = httpx.Timeout(limit, connect=min(limit, 5.0), pool=min(limit, 3.0))
        with self.client.stream("POST", endpoint, json=payload, timeout=timeout) as response:
            status = response.status_code
            if status == 401:
                if endpoint == "chat/completions":
                    self.auth_failed.set()
                elif endpoint == "rerank":
                    self.rerank_auth_failed.set()
                raise ProxyError("authentication", status)
            if status == 429:
                try:
                    delay = min(3.0, max(0.3, float(response.headers.get("Retry-After", ""))))
                except ValueError:
                    delay = 0.5
                with self.lock:
                    self.throttled += 1
                    self.cooldown_until = max(self.cooldown_until, monotonic() + delay)
                raise ProxyError("rate_limit", status)
            if not 200 <= status < 300:
                raise ProxyError("http_status", status)
            body = bytearray()
            for part in response.iter_bytes():
                if monotonic() >= self.deadline:
                    raise ProxyError("deadline")
                body.extend(part)
                if len(body) > 16 * 1024 * 1024:
                    raise ProxyError("response_too_large")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise ProxyError("invalid_json") from None
        if not isinstance(result, dict):
            raise ProxyError("invalid_json")
        return result

    def rerank(self, model: str, query: str, documents: list[str], top_n: int) -> list[int]:
        expected = min(top_n, len(documents))
        result = self._post("rerank", {
            "model": model,
            "query": query,
            "documents": documents,
            "top_n": expected,
        })
        entries = result.get("results", result.get("data"))
        if not isinstance(entries, list) or not entries:
            raise ProxyError("invalid_rerank_response")
        order = []
        for entry in entries:
            index = entry.get("index") if isinstance(entry, dict) else None
            if type(index) is not int or not 0 <= index < len(documents):
                raise ProxyError("invalid_rerank_response")
            if index not in order:
                order.append(index)
        if len(order) < expected:
            raise ProxyError("invalid_rerank_response")
        return order[:expected]
