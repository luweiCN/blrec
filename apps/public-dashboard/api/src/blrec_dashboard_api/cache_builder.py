from __future__ import annotations

import logging

from .dashboard_cache import rebuild_dashboard_cache
from .database import initialize_database
from .settings import ApiSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = ApiSettings.from_environment()
    initialize_database(settings.database_target)
    revision = rebuild_dashboard_cache(
        settings.source_database_target, settings.database_target
    )
    logging.getLogger(__name__).info(
        'dashboard PostgreSQL cache published source_revision=%s', revision
    )


if __name__ == '__main__':
    main()
