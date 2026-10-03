"""Миграции откатываются и накатываются заново — иначе откат на проде сломается в самый неудобный момент."""

from alembic import command

from tests.conftest import alembic_cfg


def test_downgrade_and_upgrade_again():
    cfg = alembic_cfg()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
