from app.database import database_url, engine_options


def test_neon_url_requires_tls_even_when_missing_or_disabled():
    for query in ('', '?sslmode=disable', '?sslmode=prefer'):
        url = database_url('postgres://user:password@ep-example-pooler.neon.tech/neondb' + query)
        assert url.get_backend_name() == 'postgresql'
        assert url.query['sslmode'] == 'require'


def test_neon_keeps_stronger_tls_and_resilient_pool_settings():
    url = database_url('postgresql://user:password@ep-example.neon.tech/neondb?sslmode=verify-full')
    assert url.query['sslmode'] == 'verify-full'
    assert engine_options(url)['pool_pre_ping'] is True
    assert engine_options(url)['connect_args']['connect_timeout'] == 15
