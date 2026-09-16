"""Run migrations on the caller's connection, including async run_sync bridges."""

from alembic import context

from diffbutler.store.models import Base

context.configure(
    connection=context.config.attributes["connection"],
    target_metadata=Base.metadata,
    render_as_batch=True,
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
