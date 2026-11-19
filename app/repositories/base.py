from typing import Any
from uuid import UUID

from tortoise.exceptions import IntegrityError

from app.utils.errors import describe
from app.utils.logger import get_logger

logger = get_logger(__name__)


class BaseRepository:
    #: The Tortoise model helpers work on; subclasses set it.
    _model: Any

    @staticmethod
    def is_valid_uuid(val):
        if isinstance(val, UUID):
            return True
        try:
            UUID(str(val))
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    @staticmethod
    async def insert_ignoring_conflicts(model, objs):
        if objs:
            await model.bulk_create(objs, ignore_conflicts=True)

    @staticmethod
    def fill_blanks(obj, values):
        changed = False
        for f, v in values.items():
            if getattr(obj, f) is None and v is not None:
                setattr(obj, f, v)
                changed = True
        return changed

    @staticmethod
    def fill_empty(obj, values):
        changed = False
        for f, v in values.items():
            if getattr(obj, f) in (None, "") and v not in (None, ""):
                setattr(obj, f, v)
                changed = True
        return changed

    @staticmethod
    async def retry_on_conflict(operation, *args):
        for attempt in (1, 2):
            try:
                return await operation(*args)
            except IntegrityError as error:
                if attempt == 2:
                    raise
                logger.warning(
                    "integrity conflict in %s, retrying once: %s",
                    getattr(operation, "__name__", "an operation"),
                    describe(error),
                )
