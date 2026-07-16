import pymysql

pymysql.install_as_MySQLdb()

__version__ = "1.1.0"

from .celery import app as celery_app

__all__ = ("celery_app",)
