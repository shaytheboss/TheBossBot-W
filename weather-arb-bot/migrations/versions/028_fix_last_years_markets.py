"""Move last year's New York / London markets back to their real date.

Polymarket's 2025 slugs had no year ("highest-temperature-in-nyc-on-
september-26"). The discovery's slug probe found them again in 2026, and the
year-less date was read as this year, so every day New York and London got a
second market: closed a year ago (never priced), resolved with 2025's winner.
The shadow study and model_skill then scored the city's 2026 forecasts
against 2025's result.

Nothing is deleted. Each such market keeps its outcomes and history; only its
event_date moves back to the year of its own end date (resolution_time,
stored from the event's endDate at ingest). That takes it out of every
current window — model_skill, the shadow study, the dashboards.

Only markets whose own end date is more than 180 days from their event date
are touched; a real market ends within a day or two of its event.

Revision ID: 028_fix_last_years_markets
Revises: 027_daily_peaks
"""
from alembic import op

revision = "028_fix_last_years_markets"
down_revision = "027_daily_peaks"
branch_labels = None
depends_on = None

_CONDITION = """
    resolution_time IS NOT NULL
    AND abs(event_date - (resolution_time AT TIME ZONE 'UTC')::date) > 180
"""


def upgrade():
    op.execute(f"""
        UPDATE markets
           SET event_date = make_date(
                   extract(year FROM resolution_time AT TIME ZONE 'UTC')::int,
                   extract(month FROM event_date)::int,
                   extract(day FROM event_date)::int)
         WHERE {_CONDITION}
           AND NOT (extract(month FROM event_date) = 2 AND extract(day FROM event_date) = 29)
    """)


def downgrade():
    # Deliberately a no-op: putting last year's markets back under this
    # year's dates would reintroduce the corruption this migration removes.
    pass
