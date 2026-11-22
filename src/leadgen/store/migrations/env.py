"""Alembic environment. Run through `leads db migrate`, which passes the DATABASE_URL in memory."""

from alembic import context
from sqlalchemy import create_engine, pool

from leadgen.store.migrate import sqlalchemy_url

config = context.config
url = sqlalchemy_url(config.attributes["dsn"])

if context.is_offline_mode():  # `leads db migrate --sql`: print the SQL instead of running it
    context.configure(url=url, literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
