from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields


async def delete_duplicate_origins(apps, state_editor):
    """Keep the oldest crawl website per origin and delete the rest.

    Duplicates are ordered by created_at, then id. Their runs, pages, findings
    and events are removed by the database cascade. Leads are not touched, and
    they still link to their domain's remaining crawl website.

    This delete is irreversible: rolling back past this migration raises
    NotImplementedError, because the deleted rows are not restored.
    """
    CrawlWebsiteModel = apps.get_model("models", "CrawlWebsiteModel")
    db = state_editor.client

    rows = (
        await CrawlWebsiteModel.all()
        .using_db(db)
        .order_by("origin", "created_at", "id")
        .values_list("id", "origin")
    )

    seen = set()
    duplicate_ids = []
    for row_id, origin in rows:
        if origin in seen:
            duplicate_ids.append(row_id)
        else:
            seen.add(origin)

    for start in range(0, len(duplicate_ids), 500):
        chunk = duplicate_ids[start : start + 500]
        await CrawlWebsiteModel.filter(id__in=chunk).using_db(db).delete()


class Migration(migrations.Migration):
    dependencies = [("models", "0002_scrapes_drop_raw_html")]

    initial = False

    operations = [
        ops.RemoveConstraint(
            model_name="CrawlWebsiteModel",
            name=None,
            fields=["scrape_id", "lead_position"],
        ),
        ops.RunPython(code=delete_duplicate_origins),
        ops.AlterField(
            model_name="CrawlWebsiteModel",
            name="origin",
            field=fields.CharField(unique=True, max_length=2048),
        ),
        ops.RemoveField(model_name="CrawlWebsiteModel", name="lead_position"),
        ops.RemoveField(model_name="CrawlWebsiteModel", name="scrape_id"),
    ]
