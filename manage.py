#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "feed_on.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Could not import Django. Install dependencies with "
            "`pip install -r requirements.txt` and try again."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
