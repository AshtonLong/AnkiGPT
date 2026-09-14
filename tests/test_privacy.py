import io
import json
import zipfile

import pytest

from app.extensions import db
from app.models import Card, Deck, Figure, Source, User
from conftest import login, logout


@pytest.fixture
def accounts(app):
    result = []
    with app.app_context():
        for name in ('alice', 'bob'):
            user = User(email=f'{name}@example.com')
            user.set_password('password123')
            db.session.add(user)
            db.session.flush()
            deck = Deck(user_id=user.id, title=f'{name} private deck', card_style='basic',
                        status='ready', source_type='text', source_text=f'{name} private source',
                        settings_json={})
            db.session.add(deck)
            db.session.flush()
            source = Source(deck_id=deck.id, idx=0, text=deck.source_text, hash=name)
            figure = Figure(deck_id=deck.id, hash=name, image=f'{name} private image'.encode())
            db.session.add_all([source, figure])
            db.session.flush()
            card = Card(deck_id=deck.id, source_id=source.id, figure_id=figure.id,
                        type='basic', front=f'{name} private question', back='Answer', status='ok')
            db.session.add(card)
            db.session.flush()
            result.append({'user_id': user.id, 'deck_id': deck.id, 'card_id': card.id,
                           'figure_id': figure.id, 'email': user.email})
        db.session.commit()
    return result


def workspace_requests(app, ids, only_owned=False):
    adapter = app.url_map.bind('localhost')
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith('main.') or rule.endpoint == 'main.index':
            continue
        if only_owned and not rule.arguments:
            continue
        values = {key: ids[key] for key in rule.arguments}
        path = adapter.build(rule.endpoint, values)
        for method in sorted(rule.methods & {'GET', 'POST'}):
            yield path, method


@pytest.mark.parametrize('legacy_demo_setting', [True, False])
def test_every_workspace_route_requires_auth_before_and_after_logout(client, app, accounts, legacy_demo_setting):
    app.config['AUTH_REQUIRED'] = legacy_demo_setting
    for after_logout in (False, True):
        if after_logout:
            login(client, email=accounts[0]['email'])
            logout(client)
        for path, method in workspace_requests(app, accounts[0]):
            response = client.open(path, method=method)
            assert response.status_code == 302, (path, method)
            assert '/auth/login' in response.location
            assert 'no-store' in response.headers['Cache-Control']
        assert client.get('/').status_code == 200


def test_two_accounts_cannot_access_each_others_resources(app, accounts):
    clients = [app.test_client(), app.test_client()]
    for index, client in enumerate(clients):
        own, other = accounts[index], accounts[1 - index]
        login(client, email=own['email'])
        library = client.get('/decks').text
        assert own['email'].split('@')[0] + ' private deck' in library
        assert other['email'].split('@')[0] + ' private deck' not in library
        assert client.get(f"/decks/{own['deck_id']}").status_code == 200
        for path, method in workspace_requests(app, other, only_owned=True):
            response = client.open(path, method=method, data={'front': 'Stolen', 'action': 'replan'})
            assert response.status_code == 404, (path, method)
            assert 'private source' not in response.text
            assert 'private question' not in response.text
        with app.app_context():
            assert db.session.get(Deck, other['deck_id']).status == 'ready'
            assert db.session.get(Card, other['card_id']).front.endswith('private question')


@pytest.mark.parametrize('action', ['delete', 'restore', 'tag', 'regenerate', 'coach'])
def test_bulk_actions_ignore_foreign_cards(client, app, accounts, monkeypatch, action):
    from app.routes import main
    called = []
    monkeypatch.setattr(main, 'regenerate_source', lambda *args: called.append(args))
    monkeypatch.setattr(main, 'coach_cards', lambda *args: called.append(args))
    login(client, email=accounts[0]['email'])
    foreign = accounts[1]
    response = client.post('/cards/bulk', data={
        'card_ids': [str(foreign['card_id'])], 'action': action, 'tag': 'stolen'})
    assert response.status_code == 302
    assert called == []
    with app.app_context():
        card = db.session.get(Card, foreign['card_id'])
        assert card.status == 'ok' and card.tags == []


def test_new_deck_ignores_forged_owner_and_logout_hides_library(client, app, accounts):
    own, other = accounts
    login(client, email=own['email'])
    response = client.post('/decks/new', data={
        'title': 'New owned deck', 'source_type': 'text', 'card_style': 'basic',
        'text_input': 'Private notes.', 'user_id': other['user_id']})
    assert response.status_code == 302
    with app.app_context():
        assert Deck.query.filter_by(title='New owned deck').one().user_id == own['user_id']
    logout(client)
    login(client, email=other['email'])
    assert 'New owned deck' not in client.get('/decks').text


def test_private_images_exports_and_pages_cannot_be_http_cached(client, app, accounts):
    own = accounts[0]
    login(client, email=own['email'])
    for path, method in [('/decks', 'GET'), (f"/decks/{own['deck_id']}", 'GET'),
                         (f"/figures/{own['figure_id']}.png", 'GET'),
                         (f"/decks/{own['deck_id']}/progress.json", 'GET'),
                         (f"/decks/{own['deck_id']}/export", 'POST'), ('/auth/profile', 'GET')]:
        response = client.open(path, method=method)
        assert response.status_code == 200, path
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert 'Cookie' in response.vary


def test_export_never_includes_a_foreign_deck_image(client, app, accounts):
    own, other = accounts
    with app.app_context():
        db.session.get(Card, own['card_id']).figure_id = other['figure_id']
        db.session.commit()
    login(client, email=own['email'])
    response = client.post(f"/decks/{own['deck_id']}/export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        assert json.loads(package.read('media')) == {}


def test_deleted_account_session_cannot_access_workspace(client, app, accounts):
    login(client, email=accounts[0]['email'])
    with app.app_context():
        db.session.delete(db.session.get(User, accounts[0]['user_id']))
        db.session.commit()
    assert client.get('/decks').status_code == 302
