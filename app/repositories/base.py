from uuid import UUID

from tortoise.exceptions import IntegrityError

from app.utils.errors import describe
from app.utils.logger import get_logger
from app.utils.utils import utc_now

logger = get_logger(__name__)


class BaseRepository:
    @staticmethod
    def now():
        return utc_now()

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
    async def last_counter(model, **filters):
        last = (
            await model.filter(counter__isnull=False, **filters)
            .order_by("-counter")
            .first()
        )
        return None if last is None else last.counter

    @classmethod
    async def next_counter(cls, model, **filters):
        last = await cls.last_counter(model, **filters)
        return 0 if last is None else last + 1

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
