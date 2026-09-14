import re
import sqlite3

import pytest

from app.extensions import db
from app.models import Card, Deck, User
from conftest import login, logout, register


def test_existing_accounts_gain_nullable_profile_fields(tmp_path):
    from app import create_app
    from conftest import TestConfig

    database = tmp_path / 'legacy.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE user (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, created_at DATETIME)')
        connection.execute("INSERT INTO user (email, password_hash) VALUES ('existing@example.com', 'existing-hash')")

    class LegacyConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{database.as_posix()}'

    application = create_app(LegacyConfig)
    with application.app_context():
        user = User.query.one()
        assert user.email == 'existing@example.com' and user.password_hash == 'existing-hash'
        assert user.display_name is None and user.bio is None
        assert user.profile_color == 'terracotta'
        db.session.remove()
        db.engine.dispose()


def test_profile_requires_login_even_in_demo_mode(client, app):
    for required in (True, False):
        app.config['AUTH_REQUIRED'] = required
        response = client.get('/auth/profile')
        assert response.status_code == 302
        assert '/auth/login?next=' in response.location
        assert b'My profile' in client.get('/').data


def test_login_and_signup_return_to_profile_without_open_redirect(client):
    response = client.post('/auth/signup?next=/auth/profile', data={
        'email': 'profile@example.com', 'password': 'password123'})
    assert response.location == '/auth/profile'
    logout(client)
    response = client.post('/auth/login?next=/auth/profile', data={
        'email': 'profile@example.com', 'password': 'password123'})
    assert response.location == '/auth/profile'
    logout(client)
    response = client.post('/auth/login?next=https://example.com', data={
        'email': 'profile@example.com', 'password': 'password123'})
    assert response.location == '/decks'


def test_profile_persists_identity_and_escapes_content(client, app):
    register(client)
    response = client.post('/auth/profile', data={
        'section': 'profile', 'display_name': '  Alex Learner  ',
        'bio': '<script>alert(1)</script>', 'avatar_color': 'sage'}, follow_redirects=True)
    assert response.status_code == 200
    assert b'Your profile is saved.' in response.data
    assert b'Alex Learner' in response.data
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in response.data
    with app.app_context():
        user = User.query.one()
        assert user.display_name == 'Alex Learner'
        assert user.initials == 'AL'
        assert user.profile_color == 'sage'
    assert b'avatar-sage' in client.get('/decks').data
    logout(client)
    login(client)
    assert b'Alex Learner' in client.get('/auth/profile').data


@pytest.mark.parametrize('field,value', [
    ('display_name', 'x' * 81), ('bio', 'x' * 281), ('avatar_color', 'invalid'),
])
def test_profile_rejects_invalid_details_atomically(client, app, field, value):
    register(client)
    data = {'section': 'profile', 'display_name': 'Changed', 'bio': 'Hello', 'avatar_color': 'blue'}
    data[field] = value
    response = client.post('/auth/profile', data=data)
    assert response.status_code == 422
    with app.app_context():
        user = User.query.one()
        assert user.display_name is None and user.bio is None and user.avatar_color is None


def test_email_change_requires_password_and_rejects_duplicate(client, app):
    register(client)
    with app.app_context():
        other = User(email='taken@example.com')
        other.set_password('another-password')
        db.session.add(other)
        db.session.commit()
    for email, password in [('new@example.com', 'wrong'), ('taken@example.com', 'password123'),
                            ('invalid', 'password123')]:
        assert client.post('/auth/profile', data={
            'section': 'email', 'email': email, 'email_password': password}).status_code == 422
    with app.app_context():
        assert User.query.filter_by(email='a@example.com').count() == 1
    response = client.post('/auth/profile', data={
        'section': 'email', 'email': ' NEW@example.com ', 'email_password': 'password123'})
    assert response.status_code == 303
    logout(client)
    assert login(client, email='new@example.com').status_code == 200
    assert b'new@example.com' in client.get('/auth/profile').data


@pytest.mark.parametrize('current,new,confirmation', [
    ('wrong', 'new-password', 'new-password'),
    ('password123', 'short', 'short'),
    ('password123', 'x' * 129, 'x' * 129),
    ('password123', 'new-password', 'different'),
    ('password123', 'password123', 'password123'),
])
def test_invalid_password_change_preserves_password(client, app, current, new, confirmation):
    register(client)
    response = client.post('/auth/profile', data={
        'section': 'password', 'current_password': current,
        'new_password': new, 'confirm_password': confirmation})
    assert response.status_code == 422
    assert b'value="new-password"' not in response.data
    with app.app_context():
        assert User.query.one().check_password('password123')


def test_password_change_and_csrf(client, app):
    register(client)
    app.config['WTF_CSRF_ENABLED'] = True
    payload = {'section': 'password', 'current_password': 'password123',
               'new_password': 'new-password', 'confirm_password': 'new-password'}
    assert client.post('/auth/profile', data=payload).status_code == 400
    html = client.get('/auth/profile').text
    token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
    response = client.post('/auth/profile', data={**payload, 'csrf_token': token})
    assert response.status_code == 303
    with app.app_context():
        user = User.query.one()
        assert user.check_password('new-password')
        assert not user.check_password('password123')
    app.config['WTF_CSRF_ENABLED'] = False
    logout(client)
    assert login(client, password='new-password').status_code == 200
    assert client.get('/auth/profile').status_code == 200


def test_profile_and_workspace_use_signed_in_owner_with_legacy_demo_setting(client, app):
    app.config['AUTH_REQUIRED'] = False
    assert client.get('/decks').status_code == 302
    register(client)
    with app.app_context():
        owner = User.query.filter_by(email='a@example.com').one()
        demo = User(email='demo@local')
        demo.set_password('legacy-demo-password')
        db.session.add(demo)
        db.session.flush()
        for user, title in [(owner, 'My private deck'), (demo, 'Demo-only deck')]:
            deck = Deck(user_id=user.id, title=title, card_style='basic', status='ready',
                        source_type='text', source_text='Notes', settings_json={})
            db.session.add(deck)
            db.session.flush()
            db.session.add(Card(deck_id=deck.id, type='basic', front='Q', back='A', status='ok'))
        db.session.commit()
        demo_deck_id = deck.id
    library = client.get('/decks').data
    assert b'My private deck' in library and b'Demo-only deck' not in library
    assert client.get(f'/decks/{demo_deck_id}').status_code == 404
    profile = client.get('/auth/profile').data
    assert b'<strong>1</strong><span>Deck created' in profile
    assert b'<strong>1</strong><span>Card in your library' in profile
