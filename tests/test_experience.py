"""Behavior behind the Source to Recall screens, without paid model calls."""
import io

from app.extensions import db
from app.models import Card, Deck
from app.services import llm
from app.services.export import export_deck
from app.services.pipeline.feedback import coach_cards
from conftest import FakeLLM, register


def make_deck(client, app):
    register(client)
    client.post('/decks/new', data={'title': 'Review workflow', 'source_type': 'text',
                                  'card_style': 'mixed', 'text_input': 'A stack uses LIFO ordering.'})
    with app.app_context():
        deck = Deck.query.filter_by(title='Review workflow').one()
        deck.status = 'ready'
        cards = [Card(deck_id=deck.id, type='basic', front='Explain stacks and queues.', back='LIFO; FIFO.', status='ok'),
                 Card(deck_id=deck.id, type='cloze', cloze_text='Stacks use {{c1::LIFO}}.', status='deleted'),
                 Card(deck_id=deck.id, type='basic', front='What is LIFO?', back='Last in, first out.', status='needs_review')]
        db.session.add_all(cards)
        db.session.commit()
        return deck.id, [c.id for c in cards]


def test_invalid_source_keeps_form_values(client, app):
    register(client)
    response = client.post('/decks/new', data={'title': '', 'source_type': 'text',
                                              'card_style': 'cloze', 'text_input': 'Keep these notes.'})
    assert b'Give your deck a title' in response.data
    assert b'Keep these notes.' in response.data
    response = client.post('/decks/new', data={'title': 'Preserved title', 'source_type': 'pdf',
                                              'page_start': '8', 'page_end': '2'})
    assert b'End page must be the same as or after' in response.data
    assert b'value="Preserved title"' in response.data
    with app.app_context():
        assert Deck.query.count() == 0


def test_deleted_cloze_stays_deleted_when_edited(client, app):
    _, ids = make_deck(client, app)
    client.post(f'/cards/{ids[1]}', data={'cloze_text': 'A stack uses {{c1::LIFO}} order.', 'extra': 'Updated.'})
    with app.app_context():
        card = db.session.get(Card, ids[1])
        assert card.status == 'deleted'
        assert card.extra == 'Updated.'


def test_export_dialog_counts_only_exportable_cards(client, app):
    deck_id, _ = make_deck(client, app)
    response = client.get(f'/decks/{deck_id}')
    assert b'1 reviewed cards' in response.data
    assert b'Deleted cards and cards marked Needs review are excluded' in response.data
    response = client.post(f'/decks/{deck_id}/export')
    assert response.status_code == 200
    assert '.apkg' in response.headers['Content-Disposition']


def test_review_import_json_success_error_and_no_match(client, app):
    deck_id, ids = make_deck(client, app)
    headers = {'Accept': 'application/json'}
    response = client.post(f'/decks/{deck_id}/reviews', headers=headers)
    assert response.status_code == 422 and not response.json['ok']
    response = client.post(f'/decks/{deck_id}/reviews', headers=headers,
                           data={'anki_package': (io.BytesIO(b'not a package'), 'bad.apkg')})
    assert response.status_code == 422 and not response.json['ok']
    with app.app_context():
        package = export_deck(deck_id)[0].getvalue()
    response = client.post(f'/decks/{deck_id}/reviews', headers=headers,
                           data={'anki_package': (io.BytesIO(package), 'reviews.apkg')})
    assert response.status_code == 200 and response.json['ok']
    assert response.json['matched'] == 1 and 'view=coach' in response.json['url']
    with app.app_context():
        db.session.get(Card, ids[0]).guid = 'different-note'
        db.session.commit()
    response = client.post(f'/decks/{deck_id}/reviews', headers=headers,
                           data={'anki_package': (io.BytesIO(package), 'other.apkg')})
    assert response.status_code == 422 and 'No cards from this deck' in response.json['message']


def test_coach_preserves_original_for_rewrite_and_split(client, app, monkeypatch):
    deck_id, ids = make_deck(client, app)
    monkeypatch.setattr(llm, 'openrouter_chat', FakeLLM())
    app.config['OPENROUTER_API_KEY'] = 'test-only'
    with app.app_context():
        result = coach_cards(deck_id, [ids[0], ids[2]])
        assert result['split'] == 1 and result['rewritten'] == 1
        children = Card.query.filter_by(deck_id=deck_id, status='needs_review').all()
        for child in children:
            info = child.critic_json['coach']
            if child.id == ids[2]:
                assert info['original']['front'] == 'What is LIFO?'
            else:
                assert info['original']['front'] == 'Explain stacks and queues.'
                assert info['original_card_id'] == ids[0]
    response = client.get(f'/decks/{deck_id}?view=coach')
    assert b'Compare with original' in response.data
    assert b'Explain stacks and queues.' in response.data
    assert f'id="card-{ids[0]}"'.encode() not in response.data


def test_unchecked_figure_toggle_is_respected(client, app, monkeypatch):
    from app.routes import main
    from werkzeug.datastructures import MultiDict
    deck_id, _ = make_deck(client, app)
    monkeypatch.setattr(main, 'dispatch_generation', lambda _: None)
    client.post(f'/decks/{deck_id}/preview', data={'use_figures': 'off'})
    with app.app_context():
        assert db.session.get(Deck, deck_id).settings_json['use_figures'] is False
    client.post(f'/decks/{deck_id}/preview', data=MultiDict([('use_figures', 'off'), ('use_figures', 'on')]))
    with app.app_context():
        assert db.session.get(Deck, deck_id).settings_json['use_figures'] is True


def test_secondary_views_render_without_run_data(client, app):
    deck_id, _ = make_deck(client, app)
    for suffix in ['?view=insights', '?view=coach', '?status=deleted', '?q=does-not-exist', '/status', '/plan', '/preview']:
        assert client.get(f'/decks/{deck_id}{suffix}').status_code == 200
