"""Behavior behind the Source to Recall screens, without paid model calls."""
import io

from app.extensions import db
from app.models import Card, Deck, Figure, Source
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
    app.config['OPENROUTER_API_KEY'] = 'test-only'
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


def test_cheat_sheet_toggle_is_opt_in_and_sticks(client, app, monkeypatch):
    from app.routes import main
    deck_id, _ = make_deck(client, app)
    monkeypatch.setattr(main, 'dispatch_generation', lambda _: None)
    app.config['OPENROUTER_API_KEY'] = 'test-only'

    def phases():
        return [p['key'] for p in client.get(f'/decks/{deck_id}/progress.json').json['phases']]

    page = client.get(f'/decks/{deck_id}/preview')
    assert b'Make a cheat sheet first' in page.data
    assert b'name="cheat_sheet" data-cheat-sheet >' in page.data  # rendered unchecked
    client.post(f'/decks/{deck_id}/preview', data={'target_cards': 'auto'})
    with app.app_context():
        assert db.session.get(Deck, deck_id).settings_json['cheat_sheet'] is False
    assert 'cheatsheet' not in phases()

    client.post(f'/decks/{deck_id}/preview', data={'cheat_sheet': 'on'})
    with app.app_context():
        assert db.session.get(Deck, deck_id).settings_json['cheat_sheet'] is True
    assert phases()[:4] == ['map', 'figures', 'cheatsheet', 'plan']
    assert b'name="cheat_sheet" data-cheat-sheet checked>' in client.get(f'/decks/{deck_id}/preview').data
    # Retrying a failed run must not silently drop the setting.
    with app.app_context():
        db.session.get(Deck, deck_id).status = 'failed'
        db.session.commit()
    assert b'name="cheat_sheet" value="on"' in client.get(f'/decks/{deck_id}/status').data


def test_cheat_sheet_page_shows_the_sheet_with_its_diagrams(client, app):
    deck_id, _ = make_deck(client, app)
    # A deck generated without the option has no sheet and no link to one.
    assert client.get(f'/decks/{deck_id}/cheat-sheet').headers['Location'].endswith(f'/decks/{deck_id}')
    assert 'cheat-sheet' not in client.get(f'/decks/{deck_id}').text
    with app.app_context():
        deck = db.session.get(Deck, deck_id)
        deck.run_json = {'plan': {'tasks': []}, 'cheat_sheet': {
            'units': 2, 'figures': 1, 'chars_in': 900, 'chars_out': 120, 'kept_full': [1]}}
        figure = Figure(deck_id=deck_id, page=4, hash='h', image=b'\x89PNG', caption='A stack of plates')
        sheet = '\n'.join(['## Order', '- A **stack** is LIFO.', '  - Example: push 1, push 2, pop gives 2.',
                           '[[Figure 1]] A stack of plates', '- <script>alert(1)</script>'])
        db.session.add_all([
            figure,
            Source(deck_id=deck_id, idx=0, title='Stacks', text=sheet, hash='a', page_start=4, page_end=5),
            Source(deck_id=deck_id, idx=1, title='Queues', text='A queue is FIFO.', hash='b'),
            Source(deck_id=deck_id, idx=2, title='Bibliography', text='Skipped.', hash='c', skipped=True),
        ])
        db.session.commit()
        figure_id = figure.id
    response = client.get(f'/decks/{deck_id}/cheat-sheet')
    assert response.status_code == 200
    page = response.text
    assert '<h2>Stacks <small>p.4–5</small></h2>' in page and '<h3>Order</h3>' in page
    assert 'A <strong>stack</strong> is LIFO.' in page
    assert '<li class="sub example ">Example: push 1, push 2, pop gives 2.</li>' in page
    assert f'<img src="/figures/{figure_id}.png" alt="A stack of plates"' in page
    assert '<b>Figure 1</b> A stack of plates' in page and '[[Figure' not in page
    # The sheet is model output over an uploaded file: it is escaped, never trusted.
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page and '<script>alert(1)' not in page
    # A unit that could not be condensed says so; a skipped unit is not on the sheet.
    assert page.count('shown in full') == 1 and 'A queue is FIFO.' in page
    assert 'Bibliography' not in page
    assert '1 diagram<' in page and '900 → 120 chars' in page
    for path in (f'/decks/{deck_id}', f'/decks/{deck_id}/status', f'/decks/{deck_id}/plan'):
        assert f'/decks/{deck_id}/cheat-sheet' in client.get(path).text, path
