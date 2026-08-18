from labeler import worker_ui


def test_worker_ui_mounts_api_proxy_before_local_static_page() -> None:
    app = worker_ui.create_worker_ui_app('http://nas:8800')

    paths = [route.path for route in app.routes]
    assert paths.index('/api/training-review/heroes') < paths.index('/api/{path:path}')
    assert paths.index('/api/training-review/heroes/{label}/image') < paths.index(
        '/api/{path:path}'
    )
    assert any(route.path == '/api/{path:path}' for route in app.routes)
    assert any(route.path == '' and route.name == 'static' for route in app.routes)


def test_worker_ui_serves_hero_catalog_from_worker(monkeypatch) -> None:
    monkeypatch.setattr(
        worker_ui.hero_review,
        'hero_catalog',
        lambda: [{'label': 'Kestrel', 'name': '凯斯特'}],
    )
    app = worker_ui.create_worker_ui_app('http://nas:8800')
    endpoint = next(
        route.endpoint
        for route in app.routes
        if route.path == '/api/training-review/heroes'
    )

    assert endpoint() == {
        'heroes': [
            {
                'label': 'Kestrel',
                'name': '凯斯特',
                'image_url': '/api/training-review/heroes/Kestrel/image',
            }
        ]
    }


def test_worker_ui_proxies_api_without_exposing_worker_token() -> None:
    request = worker_ui._build_upstream_request(
        'http://nas:8800/',
        path='training-review/items/7',
        query='mode=all',
        method='PUT',
        headers={
            'Host': 'worker:8801',
            'Content-Length': '29',
            'X-Request-ID': 'review-1',
        },
        body=b'{"review_status":"confirmed"}',
    )

    assert request.full_url == ('http://nas:8800/api/training-review/items/7?mode=all')
    assert request.method == 'PUT'
    assert request.data == b'{"review_status":"confirmed"}'
    assert request.headers['X-request-id'] == 'review-1'
    assert 'Authorization' not in request.headers
    assert 'Host' not in request.headers
