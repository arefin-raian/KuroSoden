"""Read-only diagnostic for the thumbnail / backup persistence tables.

Never writes. Prints, for each relevant table: the column list, row count, and
(thumbnail_sources) a sample of the JSONB fields keys per anime_doc_id.
"""

from __future__ import annotations

import os

import asyncpg


async def main() -> None:
    dsn = "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ["POSTGRES_USER"],
        p=os.environ["POSTGRES_PASSWORD"],
        h=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        db=os.environ["POSTGRES_DB"],
    )
    conn = await asyncpg.connect(dsn)
    try:
        for table in ("thumbnail_sources", "published_post_backups"):
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = $1 ORDER BY ordinal_position", table,
            )
            print(f"== {table} columns ==")
            print(", ".join(c["column_name"] for c in cols))
            n = await conn.fetchval(f"SELECT count(*) FROM {table}")
            print(f"== {table} rows: {n}")
            print()

        print("== thumbnail_sources per anime (fields keys + image_path set?) ==")
        rows = await conn.fetch(
            "SELECT anime_doc_id, anilist_id,"
            " jsonb_object_keys(fields) AS fk, image_path IS NOT NULL AS has_path"
            " FROM thumbnail_sources ORDER BY anime_doc_id, fk LIMIT 80",
        )
        seen: dict[str, list[str]] = {}
        has_path: dict[str, bool] = {}
        for r in rows:
            seen.setdefault(r["anime_doc_id"], []).append(r["fk"])
            has_path[r["anime_doc_id"]] = has_path.get(r["anime_doc_id"], False) or r["has_path"]
        for aid, keys in seen.items():
            print(f"  {aid}: anilist entries={len(set(keys))} keys={sorted(set(keys))} image_path={has_path[aid]}")

        print()
        print("== published_post_backups: which mirrors are populated ==")
        rows = await conn.fetch(
            "SELECT anime_doc_id,"
            " image_source_url IS NOT NULL AS src,"
            " image_catbox_url IS NOT NULL AS catbox,"
            " image_telegraph_url IS NOT NULL AS telegraph,"
            " image_imgbb_url IS NOT NULL AS imgbb"
            " FROM published_post_backups ORDER BY anime_doc_id",
        )
        for r in rows:
            print(
                f"  {r['anime_doc_id']}: src={r['src']} catbox={r['catbox']}"
                f" telegraph={r['telegraph']} imgbb={r['imgbb']}",
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
