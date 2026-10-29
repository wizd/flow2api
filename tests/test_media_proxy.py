"""Authenticated proxy for cached files and allowlisted Google media URLs."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import routes
from src.core.auth import AuthManager


class FakeCache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.calls = []
        self.error = None
        self.filename = "downloaded.png"

    async def download_and_cache(self, url, media_type):
        self.calls.append((url, media_type))
        if self.error is not None:
            raise self.error
        name = self.filename if media_type == "image" else "downloaded.mp4"
        (self.cache_dir / name).write_bytes(f"cached-{media_type}".encode())
        return name


@pytest.fixture
def media_client(tmp_path, monkeypatch):
    cache = FakeCache(tmp_path)
    previous = routes.generation_handler
    routes.set_generation_handler(SimpleNamespace(file_cache=cache))
    monkeypatch.setattr(AuthManager, "verify_api_key", lambda key: key == "secret")
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    yield client, cache
    routes.set_generation_handler(previous)


def _auth():
    return {"Authorization": "Bearer secret"}


def _error(response):
    body = response.json()
    assert set(body["error"]) == {"message", "type", "code", "status_code"}
    assert body["error"]["status_code"] == response.status_code
    return body["error"]


def test_media_requires_bearer_and_ignores_query_key(media_client):
    client, cache = media_client
    url = "http://cache.example/tmp/a.png"
    missing = client.get("/v1/media", params={"url": url})
    assert missing.status_code == 401

    query_key = client.get("/v1/media", params={"url": url, "key": "secret"})
    assert query_key.status_code == 401

    wrong = client.get("/v1/media", params={"url": url}, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert cache.calls == []


def test_local_cache_file_is_served_without_looking_at_the_host(media_client, tmp_path):
    client, _cache = media_client
    (tmp_path / "a.png").write_bytes(b"png-bytes")
    response = client.get(
        "/v1/media",
        params={"url": "http://public.example/tmp/a.png"},
        headers=_auth(),
    )
    assert response.status_code == 200
    assert response.content == b"png-bytes"
    assert response.headers["content-type"].startswith("image/png")


def test_path_traversal_and_missing_cache_files(media_client):
    client, cache = media_client
    headers = _auth()
    for url in (
        "http://evil.example/tmp/../../etc/passwd",
        "http://evil.example/tmp/..%2F..%2Fetc%2Fpasswd",
        "http://evil.example/tmp/..",
        "http://evil.example/not-tmp/a.png",
    ):
        response = client.get("/v1/media", params={"url": url}, headers=headers)
        assert response.status_code == 400, url
        assert _error(response)["message"] == "不支持的媒体地址"

    missing = client.get(
        "/v1/media",
        params={"url": "https://cache.example/tmp/missing.png"},
        headers=headers,
    )
    assert missing.status_code == 404
    assert _error(missing)["message"] == "缓存文件不存在"
    assert cache.calls == []


def test_non_allowlisted_host_is_rejected(media_client):
    client, cache = media_client
    headers = _auth()
    for url in (
        "https://example.com/a.png",
        "https://notgoogleusercontent.com/a.png",
        "https://flow-content.google.evil.com/image/abc",
        "http://flow-content.google/image/abc",
    ):
        response = client.get("/v1/media", params={"url": url}, headers=headers)
        assert response.status_code == 400, url
        assert _error(response)["code"] == "unsupported_media_url"
    assert cache.calls == []


def test_allowlisted_google_url_is_downloaded_by_the_server(media_client):
    client, cache = media_client
    headers = _auth()
    image_url = "https://flow-content.google/image/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    image = client.get("/v1/media", params={"url": image_url}, headers=headers)
    assert image.status_code == 200
    assert image.content == b"cached-image"
    assert cache.calls == [(image_url, "image")]

    photo = "https://lh3.googleusercontent.com/a/photo.png"
    photo_response = client.get("/v1/media", params={"url": photo}, headers=headers)
    assert photo_response.status_code == 200
    assert cache.calls[1] == (photo, "image")

    video = "https://storage.googleapis.com/bucket/clip.mp4"
    video_response = client.get("/v1/media", params={"url": video}, headers=headers)
    assert video_response.status_code == 200
    assert video_response.content == b"cached-video"
    assert cache.calls[2] == (video, "video")

    cache.error = RuntimeError("upstream refused the signed url")
    failed = client.get("/v1/media", params={"url": image_url}, headers=headers)
    assert failed.status_code == 502
    assert _error(failed)["message"] == "下载媒体失败: upstream refused the signed url"
